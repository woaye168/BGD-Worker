/* ================= 角色 ================= */

$('#characters').innerHTML = `
  <div class="box">
    <div class="panel-head">
      <h2>角色列表</h2>
      <button class="primary" id="addCharacter">+ 新增角色</button>
    </div>
    <div class="hint">角色绑定一个 TTS 音色 + 基础音速/音调/音量。粘贴剧本时未登记的角色会用默认音色自动建档，之后可在此调整。</div>
    <div id="characterList"></div>
  </div>`;

async function reloadCharacters() {
  state.characters = await api('/api/characters');
  renderCharacters();
  updateSummary();
}

function renderCharacters() {
  const list = $('#characterList');
  if (!state.characters.length) {
    list.innerHTML = '<div class="empty"><h3>还没有角色</h3><div>点击「+ 新增角色」，或直接到「对话与合成」粘贴剧本自动建档。</div></div>';
    return;
  }
  list.innerHTML = state.characters.map(c => `
    <div class="card">
      <div class="row">
        <div class="field tight" style="min-width:150px"><label>角色名</label><strong>${escapeHtml(c.name)}</strong></div>
        <div class="field"><label>音色</label><span>${escapeHtml(voiceLabel(c.voice))}</span></div>
        <div class="field tight" style="min-width:60px"><label>语速</label>${c.rate.toFixed(2)}</div>
        <div class="field tight" style="min-width:60px"><label>音调</label>${c.pitch.toFixed(2)}</div>
        <div class="field tight" style="min-width:60px"><label>音量</label>${c.volume.toFixed(2)}</div>
        <div class="field tight" style="min-width:70px"><label>默认情感</label><span class="tag emo-${c.default_emotion}">${EMOTIONS[c.default_emotion]}</span></div>
        <div class="field tight"><label>&nbsp;</label><div class="row">
          <button class="secondary" data-act="edit" data-id="${c.id}">编辑</button>
          <button class="danger" data-act="delete" data-id="${c.id}">删除</button>
        </div></div>
      </div>
    </div>`).join('');
  list.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
    const { act, id } = b.dataset;
    if (act === 'edit') openCharacterForm(state.characters.find(x => x.id === id));
    else if (act === 'delete') deleteCharacter(id);
  }));
}

function sortedLangs() {
  const langs = [...new Set(state.voices.map(v => v.lang).filter(Boolean))];
  return langs.sort((a, b) => {
    const ia = LANG_PRIORITY.indexOf(a), ib = LANG_PRIORITY.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
}

function characterFormHtml(c) {
  const langOpts = ['<option value="">全部语言</option>']
    .concat(sortedLangs().map(l => `<option value="${l}">${l}</option>`)).join('');
  const emoOpts = Object.entries(EMOTIONS)
    .map(([k, v]) => `<option value="${k}" ${c?.default_emotion === k ? 'selected' : ''}>${v}</option>`).join('');
  return `
    <h2 class="mb-3">${c ? '编辑角色' : '新增角色'}</h2>
    <div class="field mb-2"><label>角色名 *</label><input id="cName" value="${escapeHtml(c?.name || '')}" placeholder="例：村长老李"></div>
    <div class="row mb-2">
      <div class="field tight" style="min-width:160px"><label>语言筛选</label><select id="cLang">${langOpts}</select></div>
      <div class="field"><label>搜索音色</label><input id="cVoiceSearch" placeholder="按名称/ID 过滤，如 Xiaoxiao"></div>
    </div>
    <div class="field mb-2"><label>音色 *</label><select id="cVoice" size="6" style="height:auto"></select></div>
    <div class="row mb-2">
      <div class="field"><label>语速 (0.5-2.0)</label><input id="cRate" type="number" step="0.05" min="0.5" max="2" value="${c?.rate ?? 1.0}"></div>
      <div class="field"><label>音调 (0-2.0)</label><input id="cPitch" type="number" step="0.05" min="0" max="2" value="${c?.pitch ?? 1.0}"></div>
      <div class="field"><label>音量 (0-1.0)</label><input id="cVolume" type="number" step="0.05" min="0" max="1" value="${c?.volume ?? 1.0}"></div>
      <div class="field"><label>默认情感</label><select id="cEmotion">${emoOpts}</select></div>
    </div>
    <div class="row mb-3">
      <div class="field"><label>试听文本</label><input id="cAuditionText" value="你好，旅行者，欢迎来到这片土地。"></div>
      <div class="field tight"><label>&nbsp;</label><button class="secondary" id="cAudition">试听当前音色</button></div>
    </div>
    <div class="row">
      <span class="muted" id="cVoiceCount"></span>
      <span style="flex:1"></span>
      <button class="secondary" id="cCancel">取消</button>
      <button class="primary" id="cSave">${c ? '保存' : '创建'}</button>
    </div>`;
}

function refreshVoiceOptions(currentVoice) {
  const lang = $('#cLang').value;
  const kw = $('#cVoiceSearch').value.trim().toLowerCase();
  const filtered = state.voices.filter(v =>
    (!lang || v.lang === lang) &&
    (!kw || (v.id || '').toLowerCase().includes(kw)
        || (v.name || '').toLowerCase().includes(kw)
        || (v.character || '').toLowerCase().includes(kw)));
  const groups = {};
  for (const v of filtered) {
    const k = v.engine || 'edge';
    (groups[k] = groups[k] || []).push(v);
  }
  const order = ['edge', 'local', ...Object.keys(groups).filter(k => k !== 'edge' && k !== 'local')];
  const currentFull = normalizeVoiceId(currentVoice);
  const sel = $('#cVoice');
  sel.innerHTML = order.filter(k => groups[k] && groups[k].length).map(k => {
    const label = ENGINE_LABELS[k] || k;
    const opts = groups[k].map(v => {
      const val = v.full_id || (`${k}:${v.id}`);
      const sub = v.character ? ' · ' + escapeHtml(v.character)
                 : (v.gender ? ' · ' + escapeHtml(v.gender) : '');
      const langPart = v.lang ? ` · ${escapeHtml(v.lang)}` : '';
      return `<option value="${escapeHtml(val)}" ${val === currentFull ? 'selected' : ''}>${escapeHtml(v.name || v.id)}${langPart}${sub}</option>`;
    }).join('');
    return `<optgroup label="${escapeHtml(label)}">${opts}</optgroup>`;
  }).join('');
  if (!sel.value && filtered.length) {
    const first = filtered[0];
    sel.value = first.full_id || `${first.engine || 'edge'}:${first.id}`;
  }
  $('#cVoiceCount').textContent = `${filtered.length} / ${state.voices.length} 个音色`;
}

function openCharacterForm(c = null) {
  openModal(characterFormHtml(c));
  const initialVoice = c?.voice || 'edge:zh-CN-XiaoxiaoNeural';
  if (c?.voice) {
    const v = findVoice(c.voice);
    if (v && v.lang) $('#cLang').value = v.lang;
  }
  refreshVoiceOptions(initialVoice);
  $('#cLang').addEventListener('change', () => refreshVoiceOptions($('#cVoice').value));
  $('#cVoiceSearch').addEventListener('input', () => refreshVoiceOptions($('#cVoice').value));
  $('#cCancel').onclick = closeModal;
  $('#cAudition').onclick = async () => {
    const voice = $('#cVoice').value;
    if (!voice) return toast('请先选择一个音色', 'error');
    $('#cAudition').disabled = true;
    $('#cAudition').textContent = '合成中…';
    try {
      const res = await fetch('/api/characters/audition', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          voice, text: $('#cAuditionText').value,
          emotion: $('#cEmotion').value,
          rate: parseFloat($('#cRate').value) || 1,
          pitch: parseFloat($('#cPitch').value) || 1,
          volume: parseFloat($('#cVolume').value) || 1,
        }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      new Audio(URL.createObjectURL(await res.blob())).play();
    } catch (e) { toast('试听失败：' + e.message, 'error'); }
    finally { $('#cAudition').disabled = false; $('#cAudition').textContent = '试听当前音色'; }
  };
  $('#cSave').onclick = async () => {
    const data = {
      name: $('#cName').value.trim(),
      voice: $('#cVoice').value,
      rate: parseFloat($('#cRate').value) || 1,
      pitch: parseFloat($('#cPitch').value) || 1,
      volume: parseFloat($('#cVolume').value) || 1,
      default_emotion: $('#cEmotion').value,
    };
    if (!data.name) return toast('请填写角色名', 'error');
    if (!data.voice) return toast('请选择音色', 'error');
    try {
      if (c) await api(`/api/characters/${c.id}`, { method: 'PATCH', body: JSON.stringify(data) });
      else await api('/api/characters', { method: 'POST', body: JSON.stringify(data) });
      await reloadCharacters();
      closeModal();
      toast(c ? '已更新' : '已创建', 'success');
    } catch (e) { toast(e.message, 'error'); }
  };
}

async function deleteCharacter(id) {
  if (!confirm('确定删除该角色？该角色的对话不会被删除，但会变成"未指定角色"。')) return;
  try { await api(`/api/characters/${id}`, { method: 'DELETE' }); await reloadCharacters(); toast('已删除'); }
  catch (e) { toast(e.message, 'error'); }
}

$('#addCharacter').addEventListener('click', () => openCharacterForm());

reloadCharacters().catch(e => toast(e.message, 'error'));
