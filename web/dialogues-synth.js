/* ================= 剧本导入与批量合成 ================= */

const SCRIPT_PLACEHOLDER =
`村长: 欢迎来到我们的村庄，旅行者。
村长: （担忧）最近这里不太平啊。
旅行者: 发生了什么事？
村长(平静): 北边的森林里出现了怪物。
铁匠: （愤怒）那些怪物毁了我半个铺子！
# 以 # 或 // 开头的是注释，会被跳过
铁匠: 你要是能帮忙，这把剑就归你。`;

function previewScreenplay(text) {
  const speakers = new Set();
  let lines = 0, lastHad = false;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || line.startsWith('//')) continue;
    const cands = [line.indexOf(':'), line.indexOf('：')].filter(i => i >= 0);
    if (cands.length) {
      let name = line.slice(0, Math.min(...cands)).trim().replace(/[（(].+?[）)]\s*$/, '').trim();
      if (name) { speakers.add(name); lastHad = true; lines++; }
    } else if (lastHad) { lines++; }
  }
  return { lines, speakers: [...speakers] };
}

function importModalHtml() {
  const charOpts = state.characters.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
  const emoOpts = Object.entries(EMOTIONS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
  return `
    <h2 class="mb-2">粘贴剧本导入</h2>
    <div class="hint">把策划的多角色剧本文案直接贴进来。每行 <span class="mono">角色名: 台词</span>，
      可加情感标记 <span class="mono">角色名(愤怒): …</span> 或 <span class="mono">角色名: (愤怒)…</span>。
      未登记的角色会用默认音色自动建档。</div>
    <div class="row mb-2">
      <div class="field tight" style="min-width:180px"><label>格式</label><select id="impFormat">
        <option value="screenplay">剧本（角色名: 台词）</option>
        <option value="csv">CSV（角色,情感,文本[,文件名]）</option>
        <option value="json">JSON 数组</option>
        <option value="lines">纯文本（单角色，每行一句）</option>
      </select></div>
      <div class="field"><label>场景/对话组名</label><input id="impScene" placeholder="例：村口初遇（留空则不分组）"></div>
      <div class="field tight" style="min-width:110px"><label>默认情感</label><select id="impDefaultEmo">${emoOpts}</select></div>
    </div>
    <div class="row mb-2">
      <div class="field" id="impDefaultCharWrap" style="display:none"><label>默认角色（CSV未识别/纯文本用）</label>
        <select id="impDefaultChar"><option value="">-- 不指定 --</option>${charOpts}</select></div>
      <div class="field tight"><label>自动建档</label>
        <label style="text-transform:none"><input type="checkbox" id="impAutoCreate" checked> 未登记角色自动创建</label></div>
      <div class="field tight"><label>从文件加载</label><input type="file" id="impFile" accept=".txt,.csv,.json"></div>
    </div>
    <div class="field mb-2"><label>剧本内容</label>
      <textarea id="impData" rows="11" placeholder="${escapeHtml(SCRIPT_PLACEHOLDER)}"></textarea></div>
    <div class="hint" id="impPreview">识别中…</div>
    <div class="row"><span style="flex:1"></span>
      <button class="secondary" id="impCancel">取消</button>
      <button class="primary" id="impDo">导入</button>
    </div>`;
}

function openImportModal() {
  openModal(importModalHtml());
  const fmt = $('#impFormat'), data = $('#impData');
  const syncFmtUi = () => { $('#impDefaultCharWrap').style.display = (fmt.value === 'lines' || fmt.value === 'csv') ? '' : 'none'; };
  const syncPreview = () => {
    if (fmt.value !== 'screenplay') { $('#impPreview').textContent = '非剧本格式：将按所选格式解析。'; return; }
    const r = previewScreenplay(data.value);
    $('#impPreview').textContent = data.value.trim() ? `将导入约 ${r.lines} 条对话，识别到 ${r.speakers.length} 个角色：${r.speakers.join('、') || '（无）'}` : '在上方粘贴剧本，这里会实时显示识别结果。';
  };
  fmt.addEventListener('change', () => { syncFmtUi(); syncPreview(); });
  data.addEventListener('input', syncPreview);
  syncFmtUi(); syncPreview();
  $('#impFile').addEventListener('change', async e => {
    const f = e.target.files[0]; if (!f) return;
    data.value = await f.text();
    if (f.name.endsWith('.json')) fmt.value = 'json';
    else if (f.name.endsWith('.csv')) fmt.value = 'csv';
    syncFmtUi(); syncPreview();
  });
  $('#impCancel').onclick = closeModal;
  $('#impDo').onclick = async () => {
    if (!data.value.trim()) return toast('内容为空', 'error');
    const fd = new FormData();
    fd.append('format', fmt.value); fd.append('content', data.value);
    fd.append('scene', $('#impScene').value.trim()); fd.append('default_emotion', $('#impDefaultEmo').value);
    fd.append('default_character_id', $('#impDefaultChar')?.value || '');
    fd.append('auto_create', $('#impAutoCreate').checked ? 'true' : 'false');
    $('#impDo').disabled = true;
    try {
      const res = await fetch('/api/dialogues/import', { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const j = await res.json();
      await reloadCharacters(); await reloadDialogues(); closeModal();
      const created = (j.created_characters || []);
      toast(`导入 ${j.imported} 条对话` + (created.length ? `，新建角色：${created.join('、')}` : ''), 'success', 5000);
      if (created.length) toast('新角色用了默认音色，记得到「角色配置」调整', 'info', 6000);
    } catch (e) { toast('导入失败：' + e.message, 'error'); }
    finally { $('#impDo').disabled = false; }
  };
}

/* ================= 批量合成 / 取消 / 导出 ================= */
function setGenRunning(running) {
  const btn = $('#startGen');
  btn.textContent = running ? '取消合成' : '开始合成';
  btn.className = running ? 'warn' : 'primary';
  $('#exportZip').disabled = running;
  $('#genScope').disabled = running;
}

$('#startGen').addEventListener('click', async () => {
  if (synthController) { synthController.abort(); return; }
  const scope = $('#genScope').value;
  let dialogue_ids = [];
  if (scope === 'selected') {
    dialogue_ids = selectedIds();
    if (!dialogue_ids.length) return toast('请先在列表中勾选对话', 'error');
  }
  synthController = new AbortController();
  setGenRunning(true);
  $('#progBar').style.width = '0%';
  $('#genStatus').textContent = '正在请求合成…';
  let total = 0, done = 0, ok = 0, fail = 0;
  try {
    const resp = await fetch('/api/synthesis/batch/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, dialogue_ids }), signal: synthController.signal,
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done: streamDone } = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const ev of parts) {
        const m = ev.match(/^data: (.+)$/m);
        if (!m) continue;
        const d = JSON.parse(m[1]);
        if (d.phase === 'start') {
          total = d.total;
          $('#genStatus').textContent = total ? `共 ${total} 条待合成…` : '没有符合范围的对话。';
        } else if (d.phase === 'progress') {
          done = d.index; const r = d.result;
          if (r.success) ok++; else fail++;
          $('#progBar').style.width = (done / total * 100) + '%';
          const dlg = state.dialogues.find(x => x.id === r.dialogue_id);
          const who = dlg ? (charName(dlg.character_id) || '?') : '?';
          const snippet = dlg ? dlg.text.slice(0, 16) : '';
          $('#genStatus').textContent = `合成中 ${done}/${total} · 成功 ${ok} 失败 ${fail} · 当前：${who}「${snippet}」` + (r.success ? '' : ` · ✗ ${r.error || ''}`);
        } else if (d.phase === 'done') { $('#progBar').style.width = '100%'; }
      }
    }
    $('#genStatus').textContent = `完成 · 成功 ${ok} · 失败 ${fail}` + (total ? '' : '（无可合成对话）');
    if (total) toast(`合成完成：成功 ${ok} / ${total}`, fail ? 'error' : 'success');
  } catch (e) {
    if (e.name === 'AbortError') {
      $('#genStatus').textContent = `已取消 · 已完成 ${done}/${total}（已合成的部分已保存）`;
      toast('已取消合成', 'info');
    } else {
      $('#genStatus').textContent = '失败：' + e.message;
      toast('合成失败：' + e.message, 'error');
    }
  } finally {
    synthController = null; setGenRunning(false); await reloadDialogues();
  }
});

$('#exportZip').addEventListener('click', async () => {
  const scope = $('#genScope').value;
  let dialogue_ids = [];
  if (scope === 'selected') {
    dialogue_ids = selectedIds();
    if (!dialogue_ids.length) return toast('请先勾选要导出的对话', 'error');
  }
  $('#exportZip').disabled = true;
  try {
    const res = await fetch('/api/synthesis/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, dialogue_ids }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const blob = await res.blob();
    if (blob.size < 200) { toast('导出为空：所选范围内没有已合成的音频', 'error'); return; }
    const r = await saveBlobWithChooser(blob, 'npc_voices.zip', 'application/zip');
    if (r === 'saved') toast('已保存（按场景/角色分目录 + manifest.json）', 'success', 4000);
    else if (r === 'cancelled') toast('已取消保存', 'info', 1500);
    else toast('已保存到默认下载目录', 'success', 4000);
  } catch (e) { toast('导出失败：' + e.message, 'error'); }
  finally { $('#exportZip').disabled = false; }
});

$('#pasteScript').addEventListener('click', openImportModal);
