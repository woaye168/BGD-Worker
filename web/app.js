'use strict';

const EMOTIONS = { neutral:'中性', happy:'开心', sad:'悲伤', angry:'愤怒', surprise:'惊讶', fear:'害怕', calm:'平静' };
const LANG_PRIORITY = ['zh-CN','zh-TW','zh-HK','en-US','en-GB','ja-JP','ko-KR'];
const ENGINE_LABELS = { edge: 'Edge TTS', local: '本地角色' };

const state = {
  characters: [],
  dialogues: [],
  voices: [],
  selectedIds: new Set(),
  search: '',
  dragSrcId: null,
  models: { installed: [], catalog: [], runtime: { targets: [], active_target: 'cpu' } },
};

let synthController = null;
let playback = null;

const $ = s => document.querySelector(s);
const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

function normalizeVoiceId(v) {
  if (!v) return '';
  return v.includes(':') ? v : ('edge:' + v);
}

function findVoice(idOrFullId) {
  if (!idOrFullId) return null;
  const norm = normalizeVoiceId(idOrFullId);
  return state.voices.find(v => v.full_id === norm)
      || state.voices.find(v => v.id === idOrFullId)
      || null;
}

function voiceLabel(voiceId) {
  const v = findVoice(voiceId);
  if (!v) return voiceId || '(未设置)';
  const engine = ENGINE_LABELS[v.engine] || v.engine || '?';
  return `${v.name || v.id} · ${engine}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res;
}

function toast(msg, type = 'info', ms = 3000) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

async function saveBlobWithChooser(blob, suggestedName, mime) {
  if (window.showSaveFilePicker) {
    try {
      const ext = '.' + suggestedName.split('.').pop();
      const types = mime ? [{ description: ext.slice(1).toUpperCase(),
                              accept: { [mime]: [ext] } }] : undefined;
      const handle = await window.showSaveFilePicker({ suggestedName, types });
      const w = await handle.createWritable();
      await w.write(blob);
      await w.close();
      return 'saved';
    } catch (e) {
      if (e.name === 'AbortError') return 'cancelled';
    }
  }
  saveBlob(blob, suggestedName);
  return 'fallback';
}

function openModal(html) { $('#modalContent').innerHTML = html; $('#modal').classList.add('show'); }
function closeModal() { $('#modal').classList.remove('show'); }
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    $('#' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'settings') loadSettings().catch(e => toast(e.message, 'error'));
    if (btn.dataset.tab === 'models') loadModels().catch(e => toast(e.message, 'error'));
  });
});

function updateSummary() {
  const total = state.dialogues.length;
  const done = state.dialogues.filter(d => d.audio_path).length;
  $('#statSummary').textContent = `${state.characters.length} 个角色 · ${total} 条对话 · ${done} 条已合成`;
  const dc = $('#dialogueCount');
  if (dc) dc.textContent = total ? `共 ${total} 条 · 已合成 ${done} · 待合成 ${total - done}` : '';
}

function charName(id) {
  const c = state.characters.find(x => x.id === id);
  return c ? c.name : null;
}

function bytesHuman(n) {
  if (!n) return '—';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`;
}

async function consumeSSE(response, progressBar, msgEl) {
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let lastEvt = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = block.split('\n').find(l => l.startsWith('data: '));
      if (!line) continue;
      let evt;
      try { evt = JSON.parse(line.slice(6)); } catch (_) { continue; }
      lastEvt = evt;
      if (progressBar && typeof evt.percent === 'number') {
        progressBar.style.width = evt.percent + '%';
      }
      if (msgEl) {
        const msg = evt.message || (
          evt.phase === 'downloading' && typeof evt.percent === 'number'
            ? `下载中 ${evt.percent.toFixed(1)}% (${bytesHuman(evt.received)}${evt.total ? '/' + bytesHuman(evt.total) : ''})`
            : evt.phase || '');
        msgEl.textContent = msg;
      }
      if (evt.phase === 'error') throw new Error(evt.message || '未知错误');
      if (evt.phase === 'done') return evt;
    }
  }
  return lastEvt;
}

/* ================= init ================= */
(async () => {
  try { state.voices = await api('/api/characters/voices/available'); }
  catch (e) { state.voices = []; toast('音色列表加载失败，可稍后在角色编辑里重试', 'error'); }
  if (typeof reloadCharacters === 'function') await reloadCharacters();
  if (typeof reloadDialogues === 'function') await reloadDialogues();
})();
