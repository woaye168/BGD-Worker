/* ================= 对话表单与批量操作 ================= */

function selectedIds() { return [...state.selectedIds]; }

function updateBulkToolbar() {
  const bar = $('#bulkToolbar');
  if (!bar) return;
  const n = state.selectedIds.size;
  if (!n) { bar.innerHTML = ''; return; }
  const scenes = [...new Set(state.dialogues.map(d => d.scene).filter(Boolean))];
  const charOpts = state.characters.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
  const emoOpts = Object.entries(EMOTIONS).map(([k,v]) => `<option value="${k}">${v}</option>`).join('');
  bar.innerHTML = `<div class="toolbar-bulk">
    <strong>已选 ${n} 条</strong>
    <div class="field"><label>改场景 (留空=不变)</label>
      <input id="bulkScene" list="sceneList" placeholder="例：村口初遇" value="">
      <datalist id="sceneList">${scenes.map(s => `<option value="${escapeHtml(s)}">`).join('')}</datalist>
    </div>
    <div class="field tight"><label>改情感</label><select id="bulkEmo"><option value="">不变</option>${emoOpts}</select></div>
    <div class="field tight"><label>改角色</label><select id="bulkChar"><option value="">不变</option>${charOpts}</select></div>
    <button class="primary" id="bulkApply">应用修改</button>
    <button class="secondary" id="bulkSynth">合成选中</button>
    <button class="danger" id="bulkDel">删除选中</button>
    <button class="secondary" id="bulkClear">清空选择</button>
  </div>`;
  $('#bulkApply').onclick = bulkApply;
  $('#bulkSynth').onclick = bulkSynth;
  $('#bulkDel').onclick = bulkDelete;
  $('#bulkClear').onclick = () => { state.selectedIds.clear(); renderDialogues(); };
}

async function bulkApply() {
  const patch = {};
  const sc = $('#bulkScene').value;
  if (sc !== '') patch.scene = sc.trim();
  const emo = $('#bulkEmo').value; if (emo) patch.emotion = emo;
  const ch = $('#bulkChar').value; if (ch) patch.character_id = ch;
  if (!Object.keys(patch).length) return toast('未选择任何修改项', 'error');
  const ids = selectedIds();
  try {
    const r = await api('/api/dialogues/bulk-patch', { method: 'POST', body: JSON.stringify({ ids, patch }) });
    toast(`已更新 ${r.updated}/${ids.length} 条`, 'success');
    if ('emotion' in patch || 'character_id' in patch) toast('情感/角色变更会清空对应音频，请重新合成', 'info', 4000);
    await reloadDialogues();
  } catch (e) { toast('批量修改失败：' + e.message, 'error'); }
}

function bulkSynth() {
  if (!state.selectedIds.size) return;
  $('#genScope').value = 'selected';
  $('#startGen').click();
}

async function bulkDelete() {
  const ids = selectedIds();
  if (!ids.length) return;
  if (!confirm(`确认删除选中的 ${ids.length} 条对话？此操作不可恢复。`)) return;
  let ok = 0;
  for (const id of ids) { try { await api(`/api/dialogues/${id}`, { method: 'DELETE' }); ok++; } catch (_) {} }
  state.selectedIds.clear();
  toast(`已删除 ${ok}/${ids.length} 条`, ok === ids.length ? 'success' : 'error');
  await reloadDialogues();
}

/* ============ 单条新增/编辑/删除 ============ */
function dialogueFormHtml(d) {
  const charOpts = state.characters
    .map(c => `<option value="${c.id}" ${d?.character_id === c.id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('');
  const emoOpts = Object.entries(EMOTIONS)
    .map(([k, v]) => `<option value="${k}" ${(d?.emotion || 'neutral') === k ? 'selected' : ''}>${v}</option>`).join('');
  return `
    <h2 class="mb-3">${d ? '编辑对话' : '新增对话'}</h2>
    <div class="row mb-2">
      <div class="field"><label>角色 *</label><select id="dChar"><option value="">-- 选择角色 --</option>${charOpts}</select></div>
      <div class="field tight" style="min-width:120px"><label>情感</label><select id="dEmotion">${emoOpts}</select></div>
      <div class="field"><label>场景/对话组</label><input id="dScene" value="${escapeHtml(d?.scene || '')}" placeholder="例：村口初遇"></div>
    </div>
    <div class="field mb-2"><label>对话内容 *</label><textarea id="dText" rows="4" placeholder="请输入台词…">${escapeHtml(d?.text || '')}</textarea></div>
    <div class="field mb-3"><label>导出文件名（可选，留空自动命名）</label><input id="dFilename" value="${escapeHtml(d?.filename || '')}" placeholder="npc_villager_001"></div>
    ${d ? '<div class="hint">修改台词/情感/角色会使已合成音频失效，需重新合成。</div>' : ''}
    <div class="row"><span style="flex:1"></span>
      <button class="secondary" id="dCancel">取消</button>
      <button class="primary" id="dSave">${d ? '保存' : '创建'}</button>
    </div>`;
}

function openDialogueForm(d = null) {
  if (!state.characters.length) return toast('请先创建角色，或用「粘贴剧本导入」自动建档', 'error');
  openModal(dialogueFormHtml(d));
  $('#dCancel').onclick = closeModal;
  $('#dSave').onclick = async () => {
    const data = {
      character_id: $('#dChar').value, emotion: $('#dEmotion').value,
      scene: $('#dScene').value.trim(), text: $('#dText').value,
      filename: $('#dFilename').value.trim() || null,
    };
    if (!data.character_id) return toast('请选择角色', 'error');
    if (!data.text.trim()) return toast('请填写对话内容', 'error');
    try {
      if (d) await api(`/api/dialogues/${d.id}`, { method: 'PATCH', body: JSON.stringify(data) });
      else await api('/api/dialogues', { method: 'POST', body: JSON.stringify(data) });
      await reloadDialogues(); closeModal(); toast(d ? '已更新' : '已创建', 'success');
    } catch (e) { toast(e.message, 'error'); }
  };
}

async function deleteDialogue(id) {
  if (!confirm('确定删除该对话？')) return;
  try { await api(`/api/dialogues/${id}`, { method: 'DELETE' }); await reloadDialogues(); toast('已删除'); }
  catch (e) { toast(e.message, 'error'); }
}

async function previewDialogue(id, btn) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = '合成中…';
  try {
    const res = await fetch(`/api/synthesis/preview/${id}`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    new Audio(URL.createObjectURL(await res.blob())).play();
  } catch (e) { toast('试听失败：' + e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = old; }
}

async function resynthOne(id, btn) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = '合成中…';
  try {
    const r = await api(`/api/synthesis/one/${id}`, { method: 'POST' });
    if (r.success) { toast('合成成功', 'success'); await reloadDialogues(); }
    else toast('合成失败：' + (r.error || ''), 'error');
  } catch (e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = old; }
}

const _AUDIO_MIME = { ogg: 'audio/ogg', mp3: 'audio/mpeg', wav: 'audio/wav' };

async function downloadDialogue(id, btn) {
  const d = state.dialogues.find(x => x.id === id);
  if (!d?.audio_path) return toast('该对话尚未合成', 'error');
  btn.disabled = true;
  try {
    const res = await fetch(`/api/synthesis/audio/${id}`);
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    const ext = d.audio_path.split('.').pop() || 'bin';
    const r = await saveBlobWithChooser(await res.blob(), `${d.filename || id}.${ext}`, _AUDIO_MIME[ext]);
    if (r === 'saved') toast('已保存', 'success', 2000);
    else if (r === 'cancelled') toast('已取消', 'info', 1500);
    else toast('已保存到默认下载目录', 'success', 2000);
  } catch (e) { toast('下载失败：' + e.message, 'error'); }
  finally { btn.disabled = false; }
}

$('#addDialogue').addEventListener('click', () => openDialogueForm());
