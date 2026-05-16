# 导入 GPT-SoVITS 本地 voice 模型指南

本工具的本地 TTS 引擎走 **GPT-SoVITS v2** 路线。社区分享的 V2 模型通常是一组裸文件（`*.ckpt + *.pth + 参考音频.wav`），需要补一个 `meta.json` + 重打包成 zip 才能被本工具识别。本文讲清楚怎么打包。

---

## 1. 模型从哪里下

按推荐顺序：

| 来源 | 内容 | 备注 |
|---|---|---|
| [AI-Hobbyist/GPT-SoVits-V2-models (HF)](https://huggingface.co/AI-Hobbyist/GPT-SoVits-V2-models) | 原神/星铁全角色 V2 模型 | **首选**，分类清晰；中国大陆可走 hf-mirror.com 镜像 |
| [aihobbyist/GPT-SoVITS_Model_Collection (ModelScope)](https://www.modelscope.cn/models/aihobbyist/GPT-SoVITS_Model_Collection/) | 同上镜像 | 国内访问稳，速度快 |
| [cpumaxx/SoVITS-anime-mini-tts (HF)](https://huggingface.co/cpumaxx/SoVITS-anime-mini-tts) | 通用 anime 风格，**非特定 IP** | 商用风险最低 |
| [niuboyi 论坛聚合](https://www.niuboyi.com/thread-550-1-1.html) | 国内论坛网盘整合 | 需注册 |

**版权红线**：原神/星铁/V 家/Galgame 角色模型，米哈游等版权方未授权，**内部测试 OK，发行游戏 NPC 用要自负风险**。

---

## 2. V2 模型典型文件构成

下载下来的一个角色文件夹（以"钟离"为例）通常长这样：

```
钟离/
├── 钟离_e15_s420.ckpt            ← GPT 权重（约 150MB）
├── 钟离_e15_s420.pth             ← SoVITS 权重（约 80MB）
└── 参考音频_我居然能跑这么快.wav   ← 参考音频（5-10 秒）
```

**关键看清这三类文件**：
- `.ckpt` 文件 = GPT 权重（用于文本→声学 token）
- `.pth` 文件 = SoVITS 权重（用于声学 token→波形）
- `.wav` 文件 = 参考音频（决定 voice 的音色 + 情感基调）

有的分享包还会带 `参考音频.txt` 或 README 标明参考音频对应的文本（**这段文本很重要，不要丢**）。

---

## 3. 你要准备的 meta.json

在角色文件夹根创建一个 `meta.json`，字段如下（钟离示例）：

```json
{
  "id": "zhongli",
  "engine": "local",
  "name": "钟离",
  "character": "原神",
  "language": "zh-CN",
  "license": "fan-made; for personal use only",

  "gpt_weights": "钟离_e15_s420.ckpt",
  "sovits_weights": "钟离_e15_s420.pth",
  "ref_audio": "参考音频_我居然能跑这么快.wav",
  "ref_text": "我居然能跑这么快",
  "ref_lang": "zh",
  "text_lang": "zh",

  "gpt_sovits_version": "v2"
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | **必须与目录名一致**（导入时存储层以目录名为权威）；用英文/数字/下划线，避免中文 |
| `engine` | 是 | 固定写 `"local"` |
| `name` | 是 | UI 显示名，可中文 |
| `gpt_weights` | 是 | `.ckpt` 文件名（含后缀，相对模型目录） |
| `sovits_weights` | 是 | `.pth` 文件名 |
| `ref_audio` | 是 | 参考音频文件名 |
| `ref_text` | **强烈建议** | 参考音频里实际说的话。**缺这项零样本合成质量明显差** |
| `ref_lang` | 否 | 参考音频语言：`zh`/`en`/`ja`，默认 `zh` |
| `text_lang` | 否 | 合成时输入文本语言，默认 `zh` |
| `character` | 否 | 角色出处（如"原神"） |
| `language` | 否 | 整体语言标签（如"zh-CN"） |
| `license` | 否 | 版权说明，给 UI 显示提醒 |
| `gpt_sovits_version` | 否 | 默认 `v2`；当前 runtime 仅测试 v2，其他先别填 |

### ref_text 怎么写

**最重要**：参考音频里**逐字说了什么**就写什么。比如音频是"我居然能跑这么快"，就写 `"我居然能跑这么快"`。

- 如果分享包里有 `参考音频.txt`：直接用里面的内容
- 如果只有音频文件：自己听一遍写下来；或者用任意 ASR 工具（剪映/飞书妙记/Whisper）转
- **错的 ref_text 比没有 ref_text 还糟**——会让模型按错的文本去对齐，合成质量直接崩

### 参考音频要求

- 长度 **5-10 秒**最佳；太短信息不够，太长 GPT-SoVITS 也只用前 10 秒
- **干净人声**，无背景音乐、无回声、无多人对话
- 采样率 16kHz 以上（绝大多数都是）
- WAV 格式（社区分享的基本都是）

---

## 4. 打包步骤

### 4.1 整理目录结构

最终的目录应该看起来像：

```
zhongli/
├── meta.json                        ← 你刚写的
├── 钟离_e15_s420.ckpt
├── 钟离_e15_s420.pth
└── 参考音频_我居然能跑这么快.wav
```

**注意**：
- 目录名（`zhongli`）必须等于 `meta.json.id`
- 多余的 README/txt/其他文件无影响，可以保留
- 路径不要含空格（虽然技术上能工作但减少 bug 面）

### 4.2 zip 打包

把**目录本身**（不是目录里的内容）压缩成 zip：

**Windows 资源管理器**：右键 `zhongli` 文件夹 → 发送到 → 压缩(zipped)文件夹 → 得到 `zhongli.zip`

**命令行（PowerShell）**：
```powershell
Compress-Archive -Path zhongli -DestinationPath zhongli.zip
```

✅ 正确：`zhongli.zip` 解开后是 `zhongli/meta.json + ...`
❌ 错误：`zhongli.zip` 解开后直接是 `meta.json + ...`（没外层目录）

如果不小心打错了，第二种格式也能被本工具识别——会用 zip 内根目录 + 你导入时填的 id；但**强烈建议保持目录嵌套**避免歧义。

---

## 5. 在 App 里导入

1. 打开 NPC-Voice-Gen → 「模型管理」标签
2. 确保「本地 TTS 引擎」已显示「已安装」（没装的话先点安装运行时）
3. 滚到底部「导入本地模型」区域 → 选择 zip 文件 → 上传
4. 成功后该模型会出现在「已安装模型」列表
5. 切到「角色」标签 → 新建/编辑角色 → voice 下拉选刚才导入的模型 → 保存
6. 点角色旁的「试听」按钮 → 等首次合成（CPU 模式 **5-10 分钟**，加载完后续合成快得多）

### 首次合成会很慢

第一次点试听时，runtime 子进程要加载 ~2GB 的基础模型 + voice 权重：
- CPU 模式：3-10 分钟
- GPU 模式（NVIDIA CUDA）：30-90 秒

期间 UI 显示「合成中...」不动是正常的。**别中途取消**——耐心等到出声或报错。

后续合成不需要再加载，几秒到几十秒一句。

---

## 6. 常见问题

### Q：导入后试听报"加载模型权重失败"
- 检查 `.ckpt` / `.pth` 文件是不是从 V3/V4 版本来的（与 v2 runtime 不兼容）
- 检查文件名拼写是否与 `meta.json` 完全一致（区分大小写 / 中英文符号）
- 文件可能下载损坏，重新下一次

### Q：试听报"合成返回空音频"
- 99% 是 `ref_text` 与 `ref_audio` 内容对不上 —— 重听参考音频，重新写 ref_text
- 试试把文本换成参考音频里出现过的字词

### Q：试听报"显存不足"
- 当前 runtime 默认 CPU 模式，不应该出现这个错——可能你的设置改成了 cuda 但显存不够
- 关闭浏览器/游戏等占用 GPU 的程序
- 或在「软件设置」把后端切回 cpu

### Q：每次合成都要 10 分钟
- 应该只有**第一次**慢；如果**每次都慢**，可能 runtime 子进程没保持，或频繁切换 voice
- 切换 voice 也要 10-30s 加载权重，避免频繁换角色合成

### Q：导入后模型不出现在列表
- 检查 zip 内层结构是否正确（应有外层目录）
- 检查 `meta.json` JSON 格式是否合法（用 https://jsonlint.com 校验）
- 检查 `meta.json.id` 是否与外层目录名一致

---

## 7. 一个完整的例子

下面演示用 AI-Hobbyist 的"纳西妲"模型：

### 7.1 下载

从 HuggingFace 下载到本地：`https://huggingface.co/AI-Hobbyist/GPT-SoVits-V2-models/tree/main/纳西妲`，得到（示意）：

```
纳西妲/
├── 纳西妲_e10_s310.ckpt
├── 纳西妲_e10_s310.pth
├── refs/
│   └── 纳西妲_欢迎来到须弥.wav
└── README.md
```

### 7.2 整理 + 写 meta.json

```bash
# 简化结构（把 ref 音频提到根，可选）
mv 纳西妲/refs/纳西妲_欢迎来到须弥.wav 纳西妲/
rmdir 纳西妲/refs
rm 纳西妲/README.md

# 重命名目录为英文 id（推荐）
mv 纳西妲 nahida
```

写 `nahida/meta.json`：
```json
{
  "id": "nahida",
  "engine": "local",
  "name": "纳西妲",
  "character": "原神",
  "language": "zh-CN",
  "license": "fan-made; personal use only",
  "gpt_weights": "纳西妲_e10_s310.ckpt",
  "sovits_weights": "纳西妲_e10_s310.pth",
  "ref_audio": "纳西妲_欢迎来到须弥.wav",
  "ref_text": "欢迎来到须弥",
  "ref_lang": "zh",
  "text_lang": "zh"
}
```

### 7.3 打包

```powershell
Compress-Archive -Path nahida -DestinationPath nahida.zip
```

### 7.4 导入

App → 模型管理 → 导入本地模型 → 选 `nahida.zip` → 等"导入成功"提示

### 7.5 试听

App → 角色 → 新建角色 → voice 选 `local:nahida` → 保存 → 点试听 → 等首次合成 5-10 分钟 → 出声

---

## 附：schema 完整字段索引

| 字段 | 类型 | 默认 | 用途 |
|---|---|---|---|
| id | string | — | 模型唯一 ID，与目录名一致 |
| engine | string | — | 必为 `"local"` |
| name | string | — | UI 显示名 |
| character | string | "" | 角色出处 |
| language | string | "" | 语言标签 |
| license | string | "" | 版权说明 |
| license_url | string | "" | 许可证 URL |
| description | string | "" | 描述 |
| gpt_weights | string | "gpt.ckpt" | GPT 权重文件名 |
| sovits_weights | string | "sovits.pth" | SoVITS 权重文件名 |
| ref_audio | string | "ref.wav" | 参考音频文件名 |
| ref_text | string | "" | 参考音频对应文本（**强烈建议填**） |
| ref_lang | string | "zh" | 参考音频语言 |
| text_lang | string | "zh" | 输入文本语言 |
| gpt_sovits_version | string | "v2" | GPT-SoVITS 版本（当前仅测试 v2） |
