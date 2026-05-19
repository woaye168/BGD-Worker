/* ================= 软件设置 ================= */

const TARGET_LABELS = {
  'cpu': { name: 'CPU', desc: '兜底方案，所有机器可用' },
  'amd-rocm': { name: 'AMD ROCm', desc: 'Strix Halo / AI Max 395' },
  'nvidia-cuda': { name: 'NVIDIA CUDA', desc: 'RTX 等 NVIDIA 显卡' },
};

$('#settings').innerHTML = `
  <div class="box">
    <div class="panel-head"><h2>软件设置</h2></div>
    <div class="hint">数据根目录 <span class="mono" id="setDataDir">—</span>（由启动环境决定，不可在此修改；
      所有数据库 / 音频 / 日志 / 设置文件都基于它）</div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">音频</h3>
    <div class="row mb-2">
      <div class="field"><label>自定义音频保存路径（留空 = data_dir/audio）</label>
        <input id="setAudioDir" placeholder="例：D:/projects/voices">
        <small class="muted">当前生效：<span class="mono" id="setAudioDirActive">—</span></small>
      </div>
      <div class="field tight" style="min-width:160px"><label>音频格式</label>
        <select id="setOutputFormat">
          <option value="ogg">OGG（默认，推荐）</option>
          <option value="mp3">MP3</option>
          <option value="wav">WAV</option>
        </select>
      </div>
    </div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">TTS 引擎</h3>
    <div class="row mb-2">
      <div class="field tight" style="min-width:170px"><label>默认引擎（voice 无前缀时回落）</label><select id="setEngine"><option value="edge">Edge TTS（云端，免装）</option><option value="local">本地（GPT-SoVITS 等）</option></select></div>
      <div class="field tight" style="min-width:220px"><label>推理硬件变体（runtime target）</label><select id="setLocalTarget"><option value="cpu">CPU（兜底，所有机器可用）</option><option value="amd-rocm">AMD ROCm（Strix Halo / AI Max 395）</option><option value="nvidia-cuda">NVIDIA CUDA（RTX 等 N 卡）</option></select></div>
      <div class="field tight" style="min-width:200px"><label>V4 vocoder 采样步数（性能）</label><select id="setSampleSteps"><option value="4">4 步（极速 ~1.3× rt，略损）</option><option value="8">8 步（默认，接近实时 ~0.9× rt）</option><option value="16">16 步（质量略好，慢 2×）</option><option value="32">32 步（最高质量，慢 4×）</option></select></div>
    </div>
    <div class="hint" id="targetChangeHint" style="display:none">⚠️ 切换 target 后需要重新安装对应变体的运行时，已有的其它变体不会删除。</div>
    <div class="row mb-2">
      <div class="field"><label>模型 catalog URL（GitHub Release 上的 JSON；同时供运行时清单使用）</label>
        <input id="setCatalogUrl" placeholder="https://github.com/<owner>/<repo>/releases/download/<tag>/catalog.json">
      </div>
      <div class="field tight" style="min-width:140px"><label>&nbsp;</label>
        <button class="secondary" id="setCatalogRefresh">强制刷新 catalog</button>
      </div>
    </div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">本地 TTS 运行时</h3>
    <div class="hint">多 target 可并存，切换 target 只需重新指向，不会删除已装变体。<strong>当前仅支持 Windows</strong>。</div>
    <div id="runtimeGrid" class="runtime-grid"></div>
    <div class="progress" id="rtProgressWrap" style="display:none"><div class="progress-bar" id="rtProgressBar"></div></div>
    <div class="muted" id="rtMessage"></div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">AI 对话生成（远期，未启用，仅占位）</h3>
    <div class="row mb-2">
      <div class="field tight" style="min-width:130px"><label>provider</label><input id="setAiProvider" placeholder="openai / claude / ..."></div>
      <div class="field"><label>base URL</label><input id="setAiBaseUrl" placeholder="https://api.openai.com/v1"></div>
      <div class="field tight" style="min-width:200px"><label>API key</label><input id="setAiApiKey" type="password" placeholder="sk-...（仅本地存储）"></div>
      <div class="field tight" style="min-width:140px"><label>model</label><input id="setAiModel" placeholder="gpt-4o-mini"></div>
    </div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">日志</h3>
    <div class="row mb-2">
      <div class="field tight"><label>启用日志</label><label style="text-transform:none"><input type="checkbox" id="setLogEnabled"> 开启</label></div>
      <div class="field tight" style="min-width:170px"><label>日志等级</label><select id="setLogLevel"><option value="debug">debug</option><option value="info">info</option><option value="warning">warning</option><option value="error">error</option></select></div>
      <div class="field tight"><label>写入文件</label><label style="text-transform:none"><input type="checkbox" id="setLogToFile"> 写滚动文件</label></div>
      <div class="field"><label>日志目录</label><small class="muted mono" id="setLogDir">—</small></div>
    </div>

    <h3 style="font-size:13px;color:#8b949e;margin:14px 0 6px">运行时信息</h3>
    <div class="row mb-3">
      <div class="field"><label>数据库文件</label><small class="muted mono" id="setDbFile">—</small></div>
    </div>

    <div class="hint">修改后会立即应用（日志等级 / 启用状态）；改音频路径与格式只对<strong>后续新合成</strong>生效，
      已有音频不会迁移。</div>

    <div class="row">
      <span style="flex:1"></span>
      <button class="secondary" id="setReload">放弃修改</button>
      <button class="primary" id="setSave">保存设置</button>
    </div>
  </div>`;

let _originalTarget = 'cpu';

async function loadSettings() {
  const r = await api('/api/settings');
  $('#setDataDir').textContent = r.data_dir;
  $('#setAudioDirActive').textContent = r.audio_dir;
  $('#setLogDir').textContent = r.log_dir;
  $('#setDbFile').textContent = r.db_file;
  $('#setAudioDir').value = r.settings.audio_dir_override || '';
  $('#setLogEnabled').checked = !!r.settings.log.enabled;
  $('#setLogLevel').value = r.settings.log.level || 'info';
  $('#setLogToFile').checked = !!r.settings.log.to_file;
  $('#setOutputFormat').value = r.settings.tts.output_format || 'ogg';
  $('#setEngine').value = r.settings.tts.engine || 'edge';
  $('#setLocalTarget').value = (r.settings.tts.local && r.settings.tts.local.target) || 'cpu';
  _originalTarget = $('#setLocalTarget').value;
  $('#targetChangeHint').style.display = 'none';
  $('#setSampleSteps').value = String((r.settings.tts.local && r.settings.tts.local.sample_steps) || 8);
  $('#setCatalogUrl').value = (r.settings.tts.catalog && r.settings.tts.catalog.url) || '';
  $('#setAiProvider').value = (r.settings.ai && r.settings.ai.provider) || '';
  $('#setAiBaseUrl').value = (r.settings.ai && r.settings.ai.base_url) || '';
  $('#setAiApiKey').value = (r.settings.ai && r.settings.ai.api_key) || '';
  $('#setAiModel').value = (r.settings.ai && r.settings.ai.model) || '';
  await renderRuntimeCards();
}

async function saveSettings() {
  const patch = {
    audio_dir_override: $('#setAudioDir').value.trim() || null,
    log: {
      enabled: $('#setLogEnabled').checked,
      level: $('#setLogLevel').value,
      to_file: $('#setLogToFile').checked,
    },
    tts: {
      engine: $('#setEngine').value,
      output_format: $('#setOutputFormat').value,
      local: {
        target: $('#setLocalTarget').value,
        sample_steps: parseInt($('#setSampleSteps').value, 10) || 8,
      },
      catalog: { url: $('#setCatalogUrl').value.trim() },
    },
    ai: {
      provider: $('#setAiProvider').value.trim(),
      base_url: $('#setAiBaseUrl').value.trim(),
      api_key: $('#setAiApiKey').value,
      model: $('#setAiModel').value.trim(),
    },
  };
  const btn = $('#setSave');
  btn.disabled = true;
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(patch) });
    await loadSettings();
    try { state.voices = await api('/api/characters/voices/available'); } catch (_) {}
    toast('设置已保存并应用', 'success');
  } catch (e) { toast('保存失败：' + e.message, 'error'); }
  finally { btn.disabled = false; }
}

/* ============ 运行时卡片 ============ */
async function renderRuntimeCards() {
  const grid = $('#runtimeGrid');
  grid.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const r = await api('/api/models/runtime/status');
    state.models.runtime = { targets: r.targets || [], active_target: r.active_target || 'cpu' };

    if (!r.targets || !r.targets.length) {
      grid.innerHTML = '<div class="muted">无法获取运行时状态</div>';
      return;
    }

    grid.innerHTML = r.targets.map(t => runtimeCardHtml(t, r.active_target)).join('');

      r.targets.forEach(t => {
      document.getElementById(`rt-install-${t.target}`)?.addEventListener('click', () => installRuntimeTarget(t.target));
      document.getElementById(`rt-update-${t.target}`)?.addEventListener('click', () => installRuntimeTarget(t.target));
      document.getElementById(`rt-uninstall-${t.target}`)?.addEventListener('click', () => uninstallRuntimeTarget(t.target));
    });
  } catch (e) {
    grid.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function runtimeCardHtml(t, activeTarget) {
  const info = TARGET_LABELS[t.target] || { name: t.target, desc: '' };
  const isInstalled = t.installed;
  const isActive = t.target === activeTarget;
  const classes = ['runtime-card'];
  if (isInstalled) classes.push('installed');
  if (isActive) classes.push('active');

  const badge = isActive && isInstalled ? '<span class="rt-badge active">当前使用</span>'
    : isInstalled ? '<span class="rt-badge installed">已安装</span>'
    : '<span class="rt-badge not-installed">未安装</span>';

  let verInfo = '';
  if (isInstalled && t.version) {
    verInfo = `版本 ${t.version}`;
    if (t.latest_version && t.version !== t.latest_version) {
      verInfo += ` · 最新 ${t.latest_version}`;
    }
  } else if (!isInstalled && t.latest_version) {
    verInfo = `最新版本 ${t.latest_version}`;
  }

  let actions = '';
  if (isInstalled) {
    actions = t.can_update
      ? `<button class="primary" id="rt-update-${t.target}">更新到 ${t.latest_version}</button><button class="danger" id="rt-uninstall-${t.target}">卸载</button>`
      : `<button class="secondary" disabled>已是最新</button><button class="danger" id="rt-uninstall-${t.target}">卸载</button>`;
  } else if (t.can_install) {
    actions = `<button class="primary" id="rt-install-${t.target}">安装</button>`;
  } else {
    actions = `<button class="secondary" disabled>暂不可安装</button>`;
  }

  return `
    <div class="${classes.join(' ')}">
      <div class="rt-header">
        <span class="rt-name">${escapeHtml(info.name)}</span>
        ${badge}
      </div>
      <div class="rt-info">${escapeHtml(info.desc)}${verInfo ? '<br>' + escapeHtml(verInfo) : ''}</div>
      <div class="rt-actions">${actions}</div>
    </div>`;
}

async function installRuntimeTarget(target) {
  if (!confirm(`开始下载并安装 ${TARGET_LABELS[target]?.name || target} 运行时（数据量可能 1-2 GB，需稳定网络）。继续？`)) return;
  document.querySelectorAll('#runtimeGrid button').forEach(b => b.disabled = true);
  $('#rtProgressWrap').style.display = '';
  $('#rtProgressBar').style.width = '0%';
  $('#rtMessage').textContent = '开始…';
  try {
    const r = await fetch('/api/models/runtime/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target }),
    });
    await consumeSSE(r, $('#rtProgressBar'), $('#rtMessage'));
    await renderRuntimeCards();
    toast(`${TARGET_LABELS[target]?.name || target} 运行时安装完成`, 'success');
  } catch (e) { toast('安装失败：' + e.message, 'error'); }
  finally { $('#rtProgressWrap').style.display = 'none'; $('#rtMessage').textContent = ''; }
}

async function uninstallRuntimeTarget(target) {
  if (!confirm(`确定卸载 ${TARGET_LABELS[target]?.name || target} 运行时？已下载的模型不会被删除。`)) return;
  try {
    await api('/api/models/runtime/uninstall', {
      method: 'POST',
      body: JSON.stringify({ target }),
    });
    await renderRuntimeCards();
    toast('运行时已卸载', 'success');
  } catch (e) { toast('卸载失败：' + e.message, 'error'); }
}

/* ============ 事件绑定 ============ */
$('#setSave').addEventListener('click', saveSettings);
$('#setReload').addEventListener('click', () => loadSettings().catch(e => toast(e.message, 'error')));
$('#setCatalogRefresh').addEventListener('click', async () => {
  const btn = $('#setCatalogRefresh');
  btn.disabled = true;
  try {
    const r = await api('/api/models/catalog/refresh', { method: 'POST' });
    toast(`catalog 已刷新，共 ${r.models_count} 个模型`, 'success');
  } catch (e) { toast('刷新失败：' + e.message, 'error'); }
  finally { btn.disabled = false; }
});

$('#setLocalTarget').addEventListener('change', () => {
  $('#targetChangeHint').style.display = $('#setLocalTarget').value !== _originalTarget ? '' : 'none';
});
