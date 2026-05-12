# AI 维护守则

本项目按"渐进式披露规范 v2"建立。所有 AI 修改必须遵守。

## 项目概览

- **栈**：UV (包管理) + FastAPI (HTTP) + Pydantic (类型) + edge-tts (默认 TTS) + ffmpeg (转码)
- **架构**：三层依赖单向 DAG，`adapter → logic → contract`
- **模块**：见根 `.nav` 的 `总览` 行

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
- 仓储实现必须满足 `contract/ports.py` 中的 `*Repository` Protocol，禁止业务层直接读写 JSON 文件
- `Dialogue.audio_path` 为空 ⇔ 该对话从未合成 / 合成已失效（这是"仅未合成"筛选的唯一依据）
- 编辑对话的 `text`/`emotion`/`character_id` 三个字段中任一 → 必须清空 `audio_path` 与 `synthesized_at`
- 编辑角色配置 **不会** 自动失效已合成的对话音频；如需重生成请走 "全部" 范围批量合成

## 反模式

- 直接 grep 全项目而不读 `.nav`
- "顺手优化"读到的无关代码
- 改完代码不更新导航
- 在 `logic/` 层 import `storage/`、`tts/`、`api/`
- 在 `contract/` 层 import 任何业务模块
- 给 TTS 实现绕过 `EdgeTTSEngine` 之外直接写 `import edge_tts` 到 logic 层
- 给 `Dialogue` 模型加业务行为方法（领域模型保持贫血，行为在 `*/service.py`）

## 扩展指南

### 添加新 TTS 引擎 (例如本地 REST 服务)

1. 在 `tts/` 下新增文件，例如 `rest_engine.py`
2. 实现 `contract/ports.py:TTSEngine` Protocol 的所有方法
3. 在 `api/deps.py:get_tts_engine` 根据 `config.tts.engine` 字段分支选择
4. 在 `contract/config.py:TTSSettings` 添加该引擎需要的字段（如 `rest_base_url`）
5. 更新 `tts/.nav` 的"出口"行加入新引擎类名

### 添加新情感

1. 在 `contract/models.py:Emotion` 枚举添加新成员
2. 在 `synthesis/emotion_mapper.py` 两张表中各加一条
3. (可选) 更新前端 `web/index.html` 中的情感选项
4. 跑现有 dialogue 数据兼容性 (旧 JSON 里没有该值不会破坏)
