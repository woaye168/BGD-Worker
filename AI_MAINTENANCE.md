# AI 维护守则

本项目按"渐进式披露规范 v2"建立。所有 AI 修改必须遵守。

## 项目概览

- **栈**：UV (包管理) + FastAPI (HTTP) + Pydantic (类型) + SQLite (持久化, stdlib)
  + edge-tts (云端 TTS) + 本地 TTS 引擎脚手架(GPT-SoVITS, 运行时按需下载, 仅 Win)
  + stdlib urllib (catalog/运行时下载) + imageio-ffmpeg (捆绑 ffmpeg)
  + pywebview (桌面窗口) + PyInstaller (打包) + Python stdlib logging (日志)
- **架构**：三层依赖单向 DAG，`adapter → logic → contract`
- **模块**：见根 `.nav` 的 `总览` 行
- **入口**：`desktop.py` 桌面应用 / `main.py` 开发服务 / `build.py` 打包脚本
- **运行时根**：`AppConfig.data_dir` 派生 `app.db` / `audio/` / `logs/` / `settings.json` /
  `models/<id>/` (模型) / `runtimes/local-tts/` (本地 TTS 运行时)
- **CI**：`.github/workflows/ci.yml` (push 任意分支跑冒烟) + `release.yml`
  (仅 `v*` tag 触发, runs-on windows-latest, PyInstaller 打包后自动发布到 GitHub Release;
  其它平台暂不支持)

## 工作模式判断

- 单文件局部改动 / 单模块内增删 → 用「迭代模式」
- 跨 3 个及以上模块、契约破坏性变更、深度重构 → 用「重构模式」
- 不确定时默认迭代模式，遇到熔断条件再升级

熔断信号：需要修改 `contract/` 中任意类型签名 / 端口接口 / 配置字段 → 自动升级为重构模式。

## 强制流程

- 修改前：必读根 `.nav` 和目标模块 `.nav` + 受影响的 `contract/` 层文件头
- 修改中：保持 `@invariants`，跨模块只用 `@contract` 声明的接口；不要绕过 `contract/ports.py` 中的协议直接耦合实现
- 修改后：同步更新文件头 `@contract` / `@depends` / `@invariants` 和所在目录 `.nav` 的"出口"/"入口"行

## 一致性自检

开工前扫描相关 `.nav` 和文件头是否与代码一致。如不一致，**先修复，再施工**。

具体检查：
1. `.nav` 第 2 行"出口"列出的符号是否都在该目录的 `@contract` 中
2. `.nav` 第 3 行"入口"列出的路径是否真的被该目录文件 `@depends`
3. 每个 `@contract` 列的函数 / 类是否真实存在且签名一致
4. 每个 `@invariants` 是否是真实约束（被代码强制），不是 TODO 或愿望

## 关键不变量速查

(改这些行为前请重新阅读相应模块的 `.nav` 与文件头)

- 依赖方向（adapter→logic→contract DAG）：
  - `api` (adapter) → `{character, dialogue, synthesis}` (logic) + `contract`
  - `storage` (adapter) → `contract`
  - `tts` (adapter) → `synthesis` (logic) + `contract`
  - `{character, dialogue, synthesis}` (logic) → 仅 `contract`
  - 禁止反向、禁止 logic ↔ adapter 互引
- TTS 引擎实现必须满足 `contract/ports.py:TTSEngine` Protocol，禁止在 `tts/*` 之外创建 TTS 子类
- 仓储实现必须满足 `contract/ports.py` 中的 `*Repository` / `ModelStore` Protocol，禁止业务层直接读写 DB / 文件
- `Character.voice` 字符串语义：`"engine:raw_id"` 形式；无 `:` 视为 `edge:<raw_id>`
  （向后兼容存量裸 voice id）。**剥前缀只发生在 `tts/dispatch_engine`**；
  子引擎接收的 `voice` 已是裸 id，禁止子引擎自己解析 `:` 前缀
- `api/deps.py:get_tts_engine` 返回的是 `DispatchTTSEngine`（含 edge + local 子引擎），
  不是单一引擎；新增 TTS 引擎要进 `sub_engines` 字典而非取代它
- 模型/运行时变更（下载/导入/删除/安装/卸载）后必须调 `api.deps:invalidate_caches()`，
  让 dispatch 引擎与 voice 列表与新状态对齐
- 本地 TTS 运行时仅支持 Windows（用户产品决策）；其它平台 `LocalTTSEngine.synthesize` /
  `LocalTTSRuntimeInstaller.install` 抛 `TTSError`/`ModelError` 含"暂不支持"友好提示
- `Dialogue.audio_path` 为空 ⇔ 该对话从未合成 / 合成已失效（这是"仅未合成"筛选的唯一依据）
- 编辑对话的 `text`/`emotion`/`character_id` 三个字段中任一 → 必须清空 `audio_path` 与 `synthesized_at`
- 编辑角色配置 **不会** 自动失效已合成的对话音频；如需重生成请走 "全部" 范围批量合成
- 对话顺序在 SQLite 由 `position INTEGER` 列显式表达；`list()` 按 position 升序；
  `bulk_add`/`upsert(新增)` 取 MAX+1；`reorder(ids)` 重写 position（集合不匹配抛 StorageError）
- 旧 JSON 数据迁移仅在 `data_dir/*.json` 存在且对应表为空时一次性执行，迁移后归档为 `*.json.bak`（幂等）
- `AppConfig.data_dir` 是唯一数据根；派生 `audio_dir`(可被 audio_dir_override 覆盖) /
  `db_file` (app.db) / `settings_file` (settings.json) / `log_dir` (logs/) 路径
  冻结模式(`sys.frozen`)落用户家目录，否则 `./data`，`NPC_VOICE_DATA_DIR` 可覆盖；
  data_dir **不可由 API 修改**，只能通过环境变量或安装位置决定
- `AppConfig.load()` 从 `settings_file` 读用户运行时设置，缺失/损坏回退默认；`save()` 原子写
  不持久化 `data_dir` 字段（避免循环依赖）
- 静态前端目录：冻结模式从 `sys._MEIPASS/web` 解析，否则项目根 `web/`（见 `api/app.py:_web_dir`）
- 音频格式由 `TTSSettings.output_format` 决定；ffmpeg 三级探测（显式→系统 PATH→imageio-ffmpeg），
  要求 ogg/wav 但无 ffmpeg 时引擎构造期自动降级 mp3，**绝不因缺 ffmpeg 而完全不可用**
- 落盘文件扩展名取自 `TTSEngine.output_extension`，禁止在编排/路由层硬编码格式
- 剧本导入时未登记角色是否自动建档由 `api/routes_dialogue.py` 的 resolver 决定（`auto_create` 表单字段），
  `importer` 本身保持纯函数、不触碰 `CharacterService`
- 业务模块约定 `logger = logging.getLogger(__name__)`；setup_logging 由 `create_app()` 启动期调用，
  设置变更后 `routes_settings` 会再次调用以刷新 root handler；`enabled=False` 时 root level 设 CRITICAL+1（全静默）
- 设置变更后必须调用 `api.deps:invalidate_caches()` 清空 lru_cache，否则单例（含 config/audio_store/tts/
  model_store/catalog/runtime_installer）会保留旧值；audio_dir_override 变更**只影响下次新合成的
  落盘位置**，既有 audio_path 不迁移
- 模型 catalog JSON 形状：`{"version": str, "windows_x64": {download_url, sha256, size_bytes},
  "models": [{id, engine, name, character, license, download_url, sha256, size_bytes, ...}]}`；
  `windows_x64` 段供 `RuntimeInstaller` 消费，`models` 段供 `ModelCatalog` 消费；
  二者共用同一个 `tts.catalog.url`
- 模型目录约定：`data_dir/models/<model_id>/` 必含 `meta.json`（id/engine/name 必填，
  license/character/language 选填）；其余文件由 `FileSystemModelStore` 扫描记入 files/size_bytes，
  以**目录名**为权威（覆盖 meta.json 中的 id 字段）

## 反模式

- 直接 grep 全项目而不读 `.nav`
- "顺手优化"读到的无关代码
- 改完代码不更新导航
- 在 `logic/` 层 import `storage/`、`tts/`、`api/`
- 在 `contract/` 层 import 任何业务模块
- 给 TTS 实现绕过 `EdgeTTSEngine` 之外直接写 `import edge_tts` 到 logic 层
- 给 `Dialogue` 模型加业务行为方法（领域模型保持贫血，行为在 `*/service.py`）
- 业务层硬编码任意路径（应通过 `AppConfig` 的派生属性获取）
- 修改运行时设置后不调 `invalidate_caches()`（会拿到旧单例）
- 在前端用裸 `<a download>` 触发下载（pywebview 内嵌 webview 会吞掉，需 `saveBlobWithChooser`）

## 扩展指南

### 添加新 TTS 引擎 (作为 dispatch 的新子引擎)

1. 在 `tts/` 下新增文件，例如 `rest_engine.py`
2. 实现 `contract/ports.py:TTSEngine` Protocol 的所有方法（output_extension / synthesize / list_voices）
3. 在 `api/deps.py:get_tts_engine` 把新引擎实例加入 `DispatchTTSEngine` 的 `sub_engines` 字典
   （用一个稳定的 key，如 `"rest"`；该 key 即是 `Character.voice` 用的引擎前缀）
4. 在 `contract/config.py:TTSSettings` 添加该引擎需要的字段（如 `rest: RestSettings`）
5. 更新 `tts/.nav` 的"出口"行 + `contract/.nav` 出口行（若加了 Settings 类型）
6. **不要**改 `parse_voice` / dispatch 的解析规则；新前缀自动生效

### 添加新模型源 (例如自有 HTTP 仓库)

1. 在 `tts/` 下实现 `ModelCatalog` Protocol（参考 `tts/catalog_client.py`）
2. 在 `api/deps.py:get_catalog` 根据用户设置切实例（当前默认 `GithubReleaseCatalog`）
3. 在 catalog JSON 中提供 `models` 数组；每条至少含 `{id, name, download_url}`，
   `sha256/size_bytes/license/...` 选填
4. 模型 zip 内必须含 `meta.json`（同字段），下载流程自动校验

### 添加新情感

1. 在 `contract/models.py:Emotion` 枚举添加新成员
2. 在 `synthesis/emotion_mapper.py` 两张表中各加一条
3. 在 `dialogue/importer.py:_EMOTION_ALIASES` 加中/英文别名（供剧本/CSV 解析识别）
4. (可选) 更新前端 `web/index.html` 的 `EMOTIONS` 映射
5. 跑现有 dialogue 数据兼容性 (旧 JSON 里没有该值不会破坏)

### 添加新音频格式

1. 在 `tts/_ffmpeg.py:SUPPORTED_TRANSCODE` 加 `格式: (ffmpeg编码器, 容器)`
2. 在 `tts/edge_tts_engine.py:EdgeTTSEngine.__init__` 的合法格式集合加入新值
3. 在 `tts/local_engine.py:LocalTTSEngine.__init__` 的格式分支同步处理
4. 在 `api/routes_synthesis.py:_MEDIA_TYPES` 与 `api/routes_character.py:_MEDIA_TYPES` 加 MIME 映射
5. `contract/config.py:TTSSettings.output_format` 的注释取值范围同步更新

### 打包桌面应用 (Win 专属)

1. `uv sync` 安装运行时依赖（含 pywebview / imageio-ffmpeg）
2. `uv run python build.py` —— PyInstaller 单目录打包，产物在 `dist/NPC-Voice-Gen/`
3. 必须在 Windows 本机执行（CI 仅在 windows-latest 跑；mac/linux 暂不支持发版）
4. 开发期 Linux 可用 `python main.py` 浏览器模式调试（pywebview 需系统 GTK/WebKit）
5. CI 触发：仅 `v*` tag 推送（见 `.github/workflows/release.yml`），打包后自动发布到 GitHub Release

### 添加新的运行时设置项

1. 在 `contract/config.py` 的 `AppConfig` 或子模型（如 `LogSettings`、`TTSSettings`）加字段
2. 通过 PUT `/api/settings` 即可读写（路由用 `_deep_merge` 自动处理嵌套子对象）
3. 若变更需要副作用（如 audio_dir 改后要重建 audio_store），在 `routes_settings.update_settings`
   的"应用阶段"加对应处理（当前已 `invalidate_caches + setup_logging`）
4. 前端在 `web/index.html` 设置页添加对应 UI 控件并接到 `loadSettings/saveSettings`
