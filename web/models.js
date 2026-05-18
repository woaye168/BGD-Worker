/* ================= 模型管理 ================= */

$('#models').innerHTML = `
  <div class="box">
    <div class="panel-head">
      <h2>已安装模型</h2>
      <button class="secondary" id="installedReload">刷新</button>
    </div>
    <div id="installedList"></div>
  </div>

  <div class="box">
    <div class="panel-head">
      <h2>在线模型 catalog</h2>
      <div class="row">
        <button class="secondary" id="catalogReload">从远端刷新</button>
        <button class="secondary" id="catalogRefreshForce">强制刷新缓存</button>
      </div>
    </div>
    <div class="hint">catalog URL 在「软件设置」页配置。版权信息会在卡片上展示，下载前请自行确认授权。</div>
    <div id="catalogList"></div>
    <div class="progress" id="dlProgressWrap" style="display:none"><div class="progress-bar" id="dlProgressBar"></div></div>
    <div class="muted" id="dlMessage"></div>
  </div>

  <div class="box">
    <div class="panel-head"><h2>本地导入</h2></div>
    <div class="hint">把模型目录（须含 <span class="mono">meta.json</span> 与权重文件）打成 <span class="mono">.zip</span> 上传。系统会自动定位 meta.json 所在目录作为模型根。</div>
    <div class="row mb-2">
      <input type="file" id="modelImportFile" accept=".zip">
      <button class="secondary" id="modelImportBtn">上传并导入</button>
    </div>
  </div>`;

function modelCard(m, kind /* 'installed' | 'catalog' */) {
  const lic = m.license
    ? (m.license_url
        ? `<a class="mono" href="${escapeHtml(m.license_url)}" target="_blank" rel="noopener">${escapeHtml(m.license)}</a>`
        : `<span class="mono">${escapeHtml(m.license)}</span>`)
    : '<span class="muted">未声明</span>';
  const character = m.character ? `<span class="muted">· ${escapeHtml(m.character)}</span>` : '';
  const size = m.size_bytes ? `<span class="muted">${bytesHuman(m.size_bytes)}</span>` : '';
  const action = kind === 'installed'
    ? `<button class="danger" data-model-act="delete" data-id="${escapeHtml(m.id)}">删除</button>`
    : `<button class="primary" data-model-act="download" data-id="${escapeHtml(m.id)}">下载</button>`;
  return `
    <div class="card">
      <div class="row">
        <div class="field" style="min-width:240px">
          <label>${escapeHtml(m.engine || 'local')} · ${escapeHtml(m.source || kind)}</label>
          <strong>${escapeHtml(m.name || m.id)}</strong> ${character}
        </div>
        <div class="field tight" style="min-width:120px"><label>语言</label>${escapeHtml(m.language || '—')}</div>
        <div class="field tight" style="min-width:140px"><label>版权</label>${lic}</div>
        <div class="field tight" style="min-width:80px"><label>大小</label>${size}</div>
        <div class="field tight"><label>&nbsp;</label>${action}</div>
      </div>
      ${m.description ? `<div class="muted" style="margin-top:6px">${escapeHtml(m.description)}</div>` : ''}
    </div>`;
}

async function renderInstalled() {
  const list = await api('/api/models/installed');
  state.models.installed = list;
  const el = $('#installedList');
  if (!list.length) {
    el.innerHTML = '<div class="empty"><h3>暂无已安装模型</h3><div>从下方在线 catalog 下载，或导入本地 .zip。</div></div>';
    return;
  }
  el.innerHTML = list.map(m => modelCard(m, 'installed')).join('');
  el.querySelectorAll('button[data-model-act="delete"]').forEach(b => {
    b.addEventListener('click', () => deleteModel(b.dataset.id));
  });
}

async function renderCatalog() {
  const el = $('#catalogList');
  el.innerHTML = '<div class="muted">加载中…</div>';
  let list = [];
  try {
    list = await api('/api/models/catalog');
  } catch (e) {
    el.innerHTML = `<div class="empty"><h3>无法拉取 catalog</h3><div>${escapeHtml(e.message)}</div><div class="muted" style="margin-top:8px">请到「软件设置」配置 catalog URL。</div></div>`;
    return;
  }
  state.models.catalog = list;
  if (!list.length) {
    el.innerHTML = '<div class="empty"><h3>catalog 为空</h3></div>';
    return;
  }
  el.innerHTML = list.map(m => modelCard(m, 'catalog')).join('');
  el.querySelectorAll('button[data-model-act="download"]').forEach(b => {
    b.addEventListener('click', () => downloadModel(b.dataset.id, b));
  });
}

async function loadModels() {
  await Promise.all([renderInstalled(), renderCatalog()]);
}

async function deleteModel(id) {
  if (!confirm(`确定删除模型 ${id}？`)) return;
  try {
    await api(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' });
    state.voices = await api('/api/characters/voices/available');
    await renderInstalled();
    toast('已删除', 'success');
  } catch (e) { toast('删除失败：' + e.message, 'error'); }
}

async function downloadModel(id, btn) {
  if (btn) btn.disabled = true;
  $('#dlProgressWrap').style.display = '';
  $('#dlProgressBar').style.width = '0%';
  $('#dlMessage').textContent = '开始下载…';
  try {
    const r = await fetch(`/api/models/download/${encodeURIComponent(id)}`, { method: 'POST' });
    await consumeSSE(r, $('#dlProgressBar'), $('#dlMessage'));
    state.voices = await api('/api/characters/voices/available');
    await renderInstalled();
    toast(`模型 ${id} 下载完成`, 'success');
  } catch (e) {
    toast('下载失败：' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
    setTimeout(() => { $('#dlProgressWrap').style.display = 'none'; }, 1500);
  }
}

async function importModel() {
  const fileInput = $('#modelImportFile');
  if (!fileInput.files.length) return toast('请先选择 .zip 文件', 'error');
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  const btn = $('#modelImportBtn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/models/import', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    state.voices = await api('/api/characters/voices/available');
    await renderInstalled();
    toast('导入成功', 'success');
    fileInput.value = '';
  } catch (e) {
    toast('导入失败：' + e.message, 'error');
  } finally { btn.disabled = false; }
}

async function forceRefreshCatalog() {
  const btn = $('#catalogRefreshForce');
  btn.disabled = true;
  try {
    const r = await api('/api/models/catalog/refresh', { method: 'POST' });
    toast(`catalog 已刷新，共 ${r.models_count} 个模型`, 'success');
    await renderCatalog();
  } catch (e) {
    toast('刷新失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

$('#installedReload').addEventListener('click', () => renderInstalled().catch(e => toast(e.message, 'error')));
$('#catalogReload').addEventListener('click', () => renderCatalog().catch(e => toast(e.message, 'error')));
$('#catalogRefreshForce').addEventListener('click', () => forceRefreshCatalog().catch(e => toast(e.message, 'error')));
$('#modelImportBtn').addEventListener('click', importModel);
