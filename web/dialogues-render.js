/* ================= 对话渲染与交互 ================= */

$('#dialogues').innerHTML = `
  <div class="box">
    <div class="panel-head">
      <h2>批量合成</h2>
      <div class="row">
        <button class="secondary" id="pasteScript">粘贴剧本导入</button>
        <button class="secondary" id="addDialogue">+ 单条新增</button>
      </div>
    </div>
    <div class="row mb-2">
      <div class="field tight" style="min-width:200px">
        <label>合成范围</label>
        <select id="genScope">
          <option value="pending">仅未合成（新增/改动后）</option>
          <option value="all">全部对话（重新合成）</option>
          <option value="selected">仅勾选的对话</option>
        </select>
      </div>
      <div class="field tight">
        <label>&nbsp;</label>
        <div class="row">
          <button class="primary" id="startGen">开始合成</button>
          <button class="secondary" id="exportZip">导出 ZIP</button>
        </div>
      </div>
    </div>
    <div class="progress"><div class="progress-bar" id="progBar"></div></div>
    <div class="muted" id="genStatus">就绪。</div>
  </div>
  <div class="box">
    <div class="panel-head">
      <h2>对话列表</h2>
      <span class="muted" id="dialogueCount"></span>
    </div>
    <div id="dialogueList"></div>
  </div>`;

async function reloadDialogues() {
  state.dialogues = await api('/api/dialogues');
  const live = new Set(state.dialogues.map(d => d.id));
  for (const id of [...state.selectedIds]) if (!live.has(id)) state.selectedIds.delete(id);
  renderDialogues();
  updateSummary();
}

function visibleDialogues() {
  const s = state.search.trim().toLowerCase();
  if (!s) return state.dialogues;
  return state.dialogues.filter(d => {
    const c = (charName(d.character_id) || '').toLowerCase();
    return d.text.toLowerCase().includes(s) || c.includes(s) || (d.scene || '').toLowerCase().includes(s);
  });
}

function renderDialogues() {
  const list = $('#dialogueList');
  if (!state.dialogues.length) {
    list.innerHTML = `<div class="empty"><h3>还没有对话</h3><div>把策划写好的多角色剧本文案，点「粘贴剧本导入」直接贴进来，<br>未登记的角色会自动建档，然后就能批量合成了。</div><div style="margin-top:14px"><button class="primary" id="emptyPaste">粘贴剧本导入</button></div></div>`;
    $('#emptyPaste')?.addEventListener('click', openImportModal);
    return;
  }
  const visible = visibleDialogues();
  const searchHtml = `<div class="search-bar"><input id="dlgSearch" placeholder="搜索台词/角色/场景..." value="${escapeHtml(state.search)}"><span class="muted">${visible.length}/${state.dialogues.length} 条可见</span></div>`;
  if (!visible.length) {
    list.innerHTML = searchHtml + '<div class="empty"><h3>没有匹配结果</h3><div>清空搜索框查看全部对话。</div></div>';
    bindSearch();
    return;
  }
  const groups = [], gIdx = {};
  for (const d of visible) {
    const key = d.scene || '';
    if (!(key in gIdx)) { gIdx[key] = groups.length; groups.push({ scene: key, items: [] }); }
    groups[gIdx[key]].items.push(d);
  }
  const allVisibleSelected = visible.every(d => state.selectedIds.has(d.id));
  const selAllHtml = `<div class="row mb-2"><label style="margin:0;text-transform:none"><input type="checkbox" id="selAll" ${allVisibleSelected?'checked':''}> 全选可见 ${visible.length} 条</label><span class="muted">提示：同场景内可拖动行调整顺序</span></div>`;
  const groupsHtml = groups.map(g => `
    <div class="scene-head" data-scene="${escapeHtml(g.scene)}"><span> ${g.scene ? escapeHtml(g.scene) : '未分组'} · ${g.items.length} 条</span><span class="scene-actions"><button class="secondary" data-act="play-scene" data-scene="${escapeHtml(g.scene)}">连播本场景</button></span></div>
    <table><tbody class="scene-body" data-scene="${escapeHtml(g.scene)}">${g.items.map(d => dialogueRow(d)).join('')}</tbody></table>`).join('');
  list.innerHTML = searchHtml + selAllHtml + groupsHtml + `<div id="bulkToolbar"></div>`;
  bindSearch(); bindRowEvents(); bindDragHandlers(); updateBulkToolbar();
}

function dialogueRow(d) {
  const name = charName(d.character_id);
  const checked = state.selectedIds.has(d.id) ? 'checked' : '';
  const status = d.audio_path ? '<span class="status-done">已合成</span>' : '<span class="status-pending">待合成</span>';
  const audioActions = d.audio_path
    ? `<button class="secondary" data-act="play" data-id="${d.id}" title="试听">▶</button><button class="secondary" data-act="resynth" data-id="${d.id}" title="重新合成">↻</button><button class="secondary" data-act="download" data-id="${d.id}" title="下载">⬇</button>`
    : `<button class="secondary" data-act="preview" data-id="${d.id}">试听</button><button class="secondary" data-act="resynth" data-id="${d.id}">合成</button>`;
  return `<tr draggable="true" data-id="${d.id}" data-scene="${escapeHtml(d.scene||'')}">
    <td style="width:24px" class="drag-handle" title="拖动调整顺序">⋮⋮</td>
    <td style="width:28px"><input type="checkbox" class="dialogChk" data-id="${d.id}" ${checked}></td>
    <td style="width:130px">${name ? escapeHtml(name) : '<span class="status-error">未指定</span>'}</td>
    <td style="width:60px"><span class="tag emo-${d.emotion}">${EMOTIONS[d.emotion]}</span></td>
    <td style="white-space:pre-wrap">${escapeHtml(d.text)}</td>
    <td style="width:78px">${status}</td>
    <td style="width:240px"><div class="row">${audioActions}<button class="secondary" data-act="edit" data-id="${d.id}">编辑</button><button class="danger" data-act="del" data-id="${d.id}">删</button></div></td>
  </tr>`;
}

function bindSearch() {
  const inp = $('#dlgSearch'); if (!inp) return;
  inp.addEventListener('input', () => {
    state.search = inp.value;
    const caret = inp.selectionStart;
    renderDialogues();
    const ni = $('#dlgSearch');
    if (ni) { ni.focus(); try { ni.setSelectionRange(caret, caret); } catch (e) {} }
  });
}

function bindRowEvents() {
  const list = $('#dialogueList');
  list.querySelectorAll('.dialogChk').forEach(cb => cb.addEventListener('change', e => {
    if (e.target.checked) state.selectedIds.add(e.target.dataset.id);
    else state.selectedIds.delete(e.target.dataset.id);
    updateBulkToolbar();
  }));
  const selAll = $('#selAll');
  if (selAll) selAll.addEventListener('change', e => {
    visibleDialogues().forEach(d => { if (e.target.checked) state.selectedIds.add(d.id); else state.selectedIds.delete(d.id); });
    renderDialogues();
  });
  list.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
    const { act, id, scene } = b.dataset;
    if (act === 'edit') openDialogueForm(state.dialogues.find(x => x.id === id));
    else if (act === 'del') deleteDialogue(id);
    else if (act === 'play') new Audio(`/api/synthesis/audio/${id}`).play().catch(() => toast('播放失败', 'error'));
    else if (act === 'preview') previewDialogue(id, b);
    else if (act === 'resynth') resynthOne(id, b);
    else if (act === 'download') downloadDialogue(id, b);
    else if (act === 'play-scene') playScene(scene, b);
  }));
}

function bindDragHandlers() {
  const list = $('#dialogueList');
  list.querySelectorAll('tr[draggable="true"]').forEach(tr => {
    tr.addEventListener('dragstart', e => {
      state.dragSrcId = tr.dataset.id; tr.classList.add('drag-source');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', tr.dataset.id); } catch (_) {}
    });
    tr.addEventListener('dragend', () => {
      tr.classList.remove('drag-source');
      list.querySelectorAll('.drag-over-top, .drag-over-bottom').forEach(el => el.classList.remove('drag-over-top','drag-over-bottom'));
      state.dragSrcId = null;
    });
    tr.addEventListener('dragover', e => {
      if (!state.dragSrcId || tr.dataset.id === state.dragSrcId) return;
      const srcTr = list.querySelector(`tr[data-id="${state.dragSrcId}"]`);
      if (!srcTr || srcTr.parentElement !== tr.parentElement) return;
      e.preventDefault();
      const rect = tr.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      tr.classList.remove('drag-over-top','drag-over-bottom');
      tr.classList.add(before ? 'drag-over-top' : 'drag-over-bottom');
    });
    tr.addEventListener('dragleave', () => { tr.classList.remove('drag-over-top','drag-over-bottom'); });
    tr.addEventListener('drop', async e => {
      e.preventDefault();
      const srcId = state.dragSrcId, tgtId = tr.dataset.id;
      if (!srcId || srcId === tgtId) return;
      const srcTr = list.querySelector(`tr[data-id="${srcId}"]`);
      if (!srcTr || srcTr.parentElement !== tr.parentElement) { toast('跨场景请用"批量工具栏 → 改场景"', 'info'); return; }
      const before = tr.classList.contains('drag-over-top');
      tr.classList.remove('drag-over-top','drag-over-bottom');
      const srcIdx = state.dialogues.findIndex(d => d.id === srcId);
      const [moved] = state.dialogues.splice(srcIdx, 1);
      const tgtIdx = state.dialogues.findIndex(d => d.id === tgtId);
      state.dialogues.splice(before ? tgtIdx : tgtIdx + 1, 0, moved);
      renderDialogues();
      try {
        await api('/api/dialogues/reorder', { method: 'POST', body: JSON.stringify({ ids: state.dialogues.map(d => d.id) }) });
      } catch (e) { toast('保存顺序失败：' + e.message, 'error'); await reloadDialogues(); }
    });
  });
}

async function playScene(sceneKey, btn) {
  if (playback) {
    playback.cancelled = true;
    try { playback.audio.pause(); } catch (_) {}
    playback = null;
    if (btn) btn.textContent = '连播本场景';
    document.querySelectorAll('.row-playing').forEach(tr => tr.classList.remove('row-playing'));
    document.querySelectorAll('.scene-head.playing').forEach(h => h.classList.remove('playing'));
    toast('已停止连播'); return;
  }
  const items = state.dialogues.filter(d => (d.scene || '') === sceneKey && d.audio_path);
  const skipped = state.dialogues.filter(d => (d.scene || '') === sceneKey && !d.audio_path).length;
  if (!items.length) return toast('本场景没有已合成的音频可连播', 'error');
  const ctx = { audio: new Audio(), cancelled: false, sceneKey };
  playback = ctx;
  if (btn) btn.textContent = '停止连播';
  const head = document.querySelector(`.scene-head[data-scene="${CSS.escape(sceneKey)}"]`);
  if (head) head.classList.add('playing');
  for (let i = 0; i < items.length; i++) {
    if (ctx.cancelled) break;
    const d = items[i];
    const row = document.querySelector(`tr[data-id="${d.id}"]`);
    if (row) row.classList.add('row-playing');
    ctx.audio.src = `/api/synthesis/audio/${d.id}`;
    await new Promise(resolve => { ctx.audio.onended = resolve; ctx.audio.onerror = resolve; ctx.audio.play().catch(resolve); });
    if (row) row.classList.remove('row-playing');
  }
  if (head) head.classList.remove('playing');
  if (btn) btn.textContent = '连播本场景';
  if (!ctx.cancelled) toast(`连播完成 ${items.length} 条` + (skipped ? `（跳过 ${skipped} 条未合成）` : ''), 'success');
  if (playback === ctx) playback = null;
}

reloadDialogues().catch(e => toast(e.message, 'error'));
