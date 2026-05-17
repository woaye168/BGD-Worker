# @purpose: 本地 TTS 运行时打包脚本（Windows-only；CI 由 release.yml 触发）
# @layer: build-tool (非 app 一部分)
# @usage:
#   python scripts/build_runtime.py [--version 1.0.0] [--python-version 3.10.11]
#                                   [--profile {minimal,full}] [--out dist]
# @output:
#   dist/local-tts-runtime-v<version>.zip
# @invariants:
#   - 仅 Windows runners：脚本断言 sys.platform == 'win32'
#   - 默认 Python 3.10.11 embeddable：GPT-SoVITS requirements.txt pin numba==0.56.4，
#     该版本只支持 Python 3.7-3.10；升级到 3.11+ 需等 GPT-SoVITS 上游解锁 numba
#   - 默认 GPT-SoVITS clone 到 release tag 20250422v4，对齐 serve.py 的 V4 配置；
#     与 base_models V4 (s1v3.ckpt + gsv-v4-pretrained/) 配套
#   - profile=minimal：仅 fastapi+uvicorn+pydantic+serve.py（≈50MB；只能 --mock 模式跑）
#   - profile=full（默认）：再加 torch(cpu) + 完整 GPT-SoVITS 源码 + V4 基础模型
#     （BERT≈1.2GB + HuBERT≈400MB + s1v3.ckpt≈155MB + gsv-v4-pretrained/≈827MB；
#      压缩后 zip ≈ 2.5-3GB，超 GitHub Release 单文件 2GB 上限 → 必须托管 HuggingFace
#      （见批 3：scripts/build_runtime.py 加 HF 上传 step）
#   - GPT-SoVITS requirements.txt 经 _filter_requirements 过滤：drop pyopenjtalk(JA, 需 CMake)、
#     jieba_fast(C ext)、opencc(C++ binding, 嵌入式 Python 缺开发库)、gradio*(webui)、
#     faster-whisper/funasr(ASR)、modelscope、WeTextProcessing/pynini 等。
#     被 drop 的包用 _create_stub_packages 建 stub（jieba_fast 透传 jieba；pyopenjtalk 调用时显式抛错）。
#     opencc 则由 opencc-python-reimplemented 纯 Python 替代，API 兼容。
#     副作用：runtime 不支持日语合成；中文/英文不受影响
#   - matplotlib 不在 GPT-SoVITS requirements.txt 中，但 V4 的 AR.modules.lr_schedulers 在模块级
#     import matplotlib.pyplot；显式加入 _SERVE_PY_INFER_DEPS 兜底
#   - tools/ 子目录谨慎删除：V4 推理路径在模块级或运行时引用 tools.i18n（process_ckpt.py）、
#     tools.audio_sr（TTS.py），而 audio_sr.py 运行时又把 tools/AP_BWE_main 加入 sys.path。
#     因此 tools/i18n 与 tools/AP_BWE_main 均保留；只删明确无关的 asr/uvr5。
#   - gradio 被 drop（webui 专用），但 tools/my_utils.py 在模块级 import gradio as gr，
#     且 gr.Warning 被 check_for_existance / check_details 使用。给 gradio 建 stub：
#     gr.Warning / gr.Error / gr.Info 变为 print，其余属性为 no-op。这样 load_audio 能正常走。
#   - 产物结构（profile=full）：
#       <zip-root>/
#         VERSION
#         serve.py
#         python/python.exe + Lib/site-packages/...（含 stub 模块）
#         GPT_SoVITS/                    ← 从官方仓库克隆 tag 20250422v4
#         tools/                         ← 含 i18n/、audio_sr.py、AP_BWE_main/ 等
#         base_models/
#           chinese-roberta-wwm-ext-large/
#           chinese-hubert-base/
#           s1v3.ckpt
#           gsv-v4-pretrained/
#             s2Gv4.pth
#             vocoder.pth
#   - VERSION 文件内容 = --version 参数；LocalTTSRuntimeInstaller 据此显示 installed_version
#   - 临时目录使用 tempfile.TemporaryDirectory：失败/中断自动清理

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("build_runtime")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVE_PY = _REPO_ROOT / "tts" / "runtime" / "serve.py"

# 最小依赖：FastAPI + uvicorn + pydantic（serve.py 直接需要）
_MINIMAL_DEPS = [
    "fastapi>=0.115",
    "uvicorn>=0.32",
    "pydantic>=2.9",
]

# Full profile 额外依赖：serve.py 推理流程需要 numpy + soundfile
# GPT-SoVITS 的其余深依赖（librosa/transformers/jieba_fast/pypinyin/LangSegment/...）
# 走 `pip install -r GPT_SoVITS/requirements.txt`（克隆后即时安装）
# matplotlib：GPT-SoVITS V4 的 AR.modules.lr_schedulers 在模块级 import matplotlib.pyplot，
# 但上游 requirements.txt 未显式声明，需手动兜底
_SERVE_PY_INFER_DEPS = ["numpy", "soundfile", "matplotlib"]

# CPU torch wheel（Windows x64，约 250MB；不含 CUDA）
# 不指定具体版本：让 pip 解析最新稳定；如需固定可加 ==2.4.0
_TORCH_DEPS = ["torch", "torchaudio"]

# 下载基础模型的工具
_HF_DOWNLOAD_DEPS = ["huggingface_hub"]

# GPT-SoVITS 仓库 + 基础模型 HF repo
_GPT_SOVITS_REPO = "https://github.com/RVC-Boss/GPT-SoVITS.git"
# Pin 到 V4 release tag。20250422v4 引入了 48kHz 输出 + 修了 V3 金属音；
# 与 serve.py V4 配置 (s1v3.ckpt + gsv-v4-pretrained/s2Gv4.pth) 对齐。
# 升级到更新 tag（如 20250606v2pro）须在 Windows 实机回归一次合成。
_GPT_SOVITS_REF = "20250422v4"
_HF_BASE_MODELS_REPO = "lj1995/GPT-SoVITS"
# V4 需要的 base models（路径以 HF repo 根目录为相对路径）：
#   - chinese-roberta-wwm-ext-large/：BERT，V2/V3/V4 共用
#   - chinese-hubert-base/：HuBERT，V2/V3/V4 共用
#   - s1v3.ckpt：V3+V4 共用 GPT 权重（V2 用的 s1bert25hz... 不下载省空间）
#   - gsv-v4-pretrained/：V4 SoVITS 主权重 (s2Gv4.pth ~769MB) + 配套 vocoder (~58MB)
_HF_BASE_MODELS_PATTERNS = [
    "chinese-roberta-wwm-ext-large/**",
    "chinese-hubert-base/**",
    "s1v3.ckpt",
    "gsv-v4-pretrained/**",
]

# Build 完成后对 base_models/ 做 fail-fast 校验。两类断言：
#   1) FILES：相对 base_models/ 必须存在的具体文件（缺 → 视为 HF 下载失败）
#   2) NONEMPTY_DIRS：目录必须存在且非空（容忍 HF 把 pytorch_model.bin 改成 model.safetensors）
# 防止 HF repo 结构悄改、pattern 静默漏文件，让 serve.py 启动时才炸。
_BASE_MODELS_REQUIRED_FILES = [
    "s1v3.ckpt",
    "gsv-v4-pretrained/s2Gv4.pth",
]
_BASE_MODELS_REQUIRED_NONEMPTY_DIRS = [
    "chinese-roberta-wwm-ext-large",  # BERT：内部含 config.json + pytorch_model.bin 或 model.safetensors
    "chinese-hubert-base",             # HuBERT：同上
    "gsv-v4-pretrained",               # V4 SoVITS 主权重 + vocoder
]

# 从 GPT-SoVITS requirements.txt 过滤掉的包。原因分两类：
#   1) 需要原生 C/C++/CMake 编译且无 Windows wheel（嵌入式 Python 上构建会炸）
#   2) 推理用不到（训练/ASR/webui/备用模型源）
# 副作用：日语合成会失效（pyopenjtalk 没了）；中文用例不受影响，jieba_fast → jieba 用 stub 透传。
_DROP_REQUIREMENTS = {
    # 日/韩语支持：pyopenjtalk 需要 CMake + OpenJTalk 源码编译，wheel 缺失
    "pyopenjtalk",
    "pyopenjtalk-prebuilt",
    # 中文 fast 分词：Cython 原生扩展；jieba 是纯 Python 兜底
    "jieba_fast",
    # opencc 官方新版为 C++ binding，嵌入式 Python 缺少头文件与 lib，构建会炸
    # 下装 opencc-python-reimplemented 作为纯 Python 替代
    "opencc",
    # WebUI：headless runtime 用不到
    "gradio",
    "gradio_client",
    # ASR/训练相关：runtime 只做推理
    "faster-whisper",
    "funasr",
    # 备用模型源：我们走 huggingface_hub
    "modelscope",
    # 重型原生文本归一化（OpenFST 等）：可选，缺失不影响基础合成
    "wetextprocessing",
    "pynini",
    "nemo-text-processing",
    "nemo_text_processing",
}

# 给被 drop 的包建 stub，避免 GPT_SoVITS 在 import 时炸（模块级 import 会失败）。
# 值是该 stub 模块对外暴露的属性列表（GPT_SoVITS 可能 `from X import Y` 形式访问）。
_STUB_PACKAGES = {
    # pyopenjtalk: 调用时显式抛错（运行时若执行 JA 合成才会触发）
    "pyopenjtalk": {
        "type": "error",
        "attrs": ["g2p", "run_frontend", "extract_fullcontext", "make_label", "load_marine_model"],
        "error_msg": "pyopenjtalk 未打包到此 runtime（仅中文/英文模型可用）",
    },
    # jieba_fast: 透传到 jieba 的 API（功能完全等价，速度略慢）
    "jieba_fast": {
        "type": "alias",
        "alias_of": "jieba",
    },
    # gradio: 被 tools/my_utils.py 模块级 import（gr.Warning 等），但推理路径只用到
    # load_audio，gr 相关调用只在 check_for_existance / check_details 里。
    # 给 gr.Warning / gr.Error / gr.Info 变 print，其余 no-op，避免 import 炸。
    "gradio": {
        "type": "custom",
        "code": (
            "# stub by build_runtime.py: gradio (no-op for headless runtime)\n"
            "import warnings\n"
            "warnings.warn('gradio stub loaded; UI features are disabled')\n"
            "\n"
            "def _noop(*args, **kwargs):\n"
            "    pass\n"
            "\n"
            "def _print_warn(msg, *args, **kwargs):\n"
"    print('[gradio stub Warning]', msg)\n"
            "\n"
            "class _FakeBlocks:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "class _FakeRow:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "class _FakeColumn:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "class _FakeTab:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "class _FakeTabItem:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "class _FakeAccordion:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): pass\n"
            "    def __getattr__(self, name): return _noop\n"
            "\n"
            "Warning = _print_warn\n"
            "Error = _print_warn\n"
            "Info = _print_warn\n"
            "Blocks = _FakeBlocks\n"
            "Row = _FakeRow\n"
            "Column = _FakeColumn\n"
            "Tab = _FakeTab\n"
            "TabItem = _FakeTabItem\n"
            "Accordion = _FakeAccordion\n"
            "Textbox = _noop\n"
            "Button = _noop\n"
            "Audio = _noop\n"
            "File = _noop\n"
            "Dropdown = _noop\n"
            "Slider = _noop\n"
            "Number = _noop\n"
            "Checkbox = _noop\n"
            "Radio = _noop\n"
            "Markdown = _noop\n"
            "HTML = _noop\n"
            "Image = _noop\n"
            "Video = _noop\n"
            "Dataframe = _noop\n"
            "Plot = _noop\n"
            "Gallery = _noop\n"
            "Model3D = _noop\n"
            "HighlightText = _noop\n"
            "Code = _noop\n"
            "JSON = _noop\n"
            "Label = _noop\n"
            "Chatbot = _noop\n"
            "__version__ = '0.0.0-stub'\n"
        ),
    },
}


def _download(url: str, target: Path) -> None:
    logger.info("download %s → %s", url, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp, target.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_zip(zip_path: Path, target_dir: Path) -> None:
    logger.info("extract %s → %s", zip_path, target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target_dir)


def _bootstrap_pip(python_dir: Path) -> Path:
    """在 embeddable Python 中安装 pip；返回 python.exe 路径。

    embeddable 自带的 python3X._pth 默认禁了 site；改成允许，再用 get-pip.py 装。
    """
    python_exe = python_dir / "python.exe"
    if not python_exe.exists():
        raise RuntimeError(f"python.exe not found in {python_dir}")

    # 允许 site 导入（pip 需要）
    pth_files = list(python_dir.glob("python*._pth"))
    if not pth_files:
        raise RuntimeError(f"no python*._pth file in {python_dir}")
    pth = pth_files[0]
    text = pth.read_text(encoding="utf-8")
    new_text = []
    found_import_site = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "#import site":
            new_text.append("import site")
            found_import_site = True
        else:
            new_text.append(line)
    if not found_import_site:
        new_text.append("import site")
    pth.write_text("\n".join(new_text) + "\n", encoding="utf-8")
    logger.info("patched %s to enable site imports", pth.name)

    # 下载 get-pip.py 并运行
    get_pip = python_dir / "get-pip.py"
    _download("https://bootstrap.pypa.io/get-pip.py", get_pip)
    logger.info("bootstrap pip ...")
    subprocess.run(
        [str(python_exe), str(get_pip), "--no-warn-script-location"],
        check=True,
        cwd=str(python_dir),
    )
    get_pip.unlink(missing_ok=True)
    return python_exe


def _pip_install(python_exe: Path, packages: list[str], *, extra_index: str | None = None) -> None:
    args = [
        str(python_exe), "-m", "pip", "install",
        "--no-warn-script-location",
        *packages,
    ]
    if extra_index:
        args.extend(["--extra-index-url", extra_index])
    logger.info("pip install: %s", " ".join(packages))
    subprocess.run(args, check=True)


def _pip_install_requirements(python_exe: Path, requirements_file: Path) -> None:
    if not requirements_file.exists():
        logger.warning("requirements 文件不存在，跳过: %s", requirements_file)
        return
    logger.info("pip install -r %s", requirements_file)
    subprocess.run(
        [
            str(python_exe), "-m", "pip", "install",
            "--no-warn-script-location",
            "-r", str(requirements_file),
        ],
        check=True,
    )


def _filter_requirements(src: Path, dst: Path, drop_names: set[str]) -> list[str]:
    """从 src 拷贝到 dst，跳过 drop_names 中的包；返回被 drop 的原始行列表（含版本约束）。

    匹配按 PEP 503 标准化（小写、`_` → `-`）；处理 `pkg`, `pkg==1.0`, `pkg>=1.0`, `pkg[extras]`,
    `# 注释`, 空行等常见形态。不处理 -e/-r/git+ 等高级形态（GPT-SoVITS requirements.txt 不该有）。
    """
    drop_norm = {d.lower().replace("_", "-") for d in drop_names}
    dropped: list[str] = []
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for raw in fin:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                fout.write(raw)
                continue
            # 包名取 `<>=!~;[\s` 前的部分
            name_match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", stripped)
            if not name_match:
                fout.write(raw)
                continue
            pkg_norm = name_match.group(1).lower().replace("_", "-")
            if pkg_norm in drop_norm:
                dropped.append(stripped)
                fout.write(f"# (dropped by build_runtime.py): {raw}")
            else:
                fout.write(raw)
    return dropped


def _site_packages_dir(python_dir: Path) -> Path:
    """嵌入式 Python 的 site-packages 路径。"""
    sp = python_dir / "Lib" / "site-packages"
    if not sp.exists():
        raise RuntimeError(f"site-packages not found: {sp}")
    return sp


def _create_stub_packages(python_dir: Path, stubs: dict[str, dict]) -> None:
    """为被 drop 的包建 stub 模块；防止 GPT_SoVITS 模块级 import 时炸。"""
    site_packages = _site_packages_dir(python_dir)
    for name, spec in stubs.items():
        pkg_dir = site_packages / name
        if pkg_dir.exists():
            # 已经被 pip 装上（例如 jieba_fast 真有 wheel 装了），不覆盖
            logger.info("stub skip: %s already exists in site-packages", name)
            continue
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init_py = pkg_dir / "__init__.py"
        if spec["type"] == "alias":
            alias_of = spec["alias_of"]
            init_py.write_text(
                "# stub by build_runtime.py: " + name + " → " + alias_of + "\n"
                "from " + alias_of + " import *  # noqa: F401, F403\n"
                "import " + alias_of + " as _real\n"
                "import sys as _sys\n"
                "_sys.modules[__name__].__dict__.update(_real.__dict__)\n",
                encoding="utf-8",
            )
            logger.info("stub created: %s → alias of %s", name, alias_of)
        elif spec["type"] == "error":
            msg = spec.get("error_msg", f"{name} unavailable in this runtime")
            attrs = spec.get("attrs", [])
            lines = [
                "# stub by build_runtime.py: " + name + " (raises on use)",
                "import warnings",
                "warnings.warn(" + repr(f"{name} stub loaded; calls will raise RuntimeError") + ")",
                "",
                "def _missing(*args, **kwargs):",
                "    raise RuntimeError(" + repr(msg) + ")",
                "",
            ]
            for attr in attrs:
                lines.append(f"{attr} = _missing")
            init_py.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info("stub created: %s (raise-on-use, %d attrs)", name, len(attrs))
        elif spec["type"] == "custom":
            init_py.write_text(spec["code"], encoding="utf-8")
            logger.info("stub created: %s (custom code)", name)


def _clone_gpt_sovits(target: Path) -> None:
    """浅克隆 GPT-SoVITS 仓库（约 50MB 源码，无 git history）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth=1", "--branch", _GPT_SOVITS_REF, _GPT_SOVITS_REPO, str(target)]
    logger.info("git clone: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    # 清理 .git 减小体积
    git_dir = target / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)


def _strip_gpt_sovits_extras(repo_root: Path) -> None:
    """移除 GPT-SoVITS 仓库中训练/微调/前端相关、推理不需要的目录与文件，节省 zip 体积。

    V4 推理路径在模块级引用的 tools/ 子目录：
      - tools.i18n (process_ckpt.py, TextPreprocessor.py)
      - tools.audio_sr (TTS.py) → 运行时又把 tools/AP_BWE_main 加入 sys.path
    因此 tools/i18n 与 tools/AP_BWE_main 均不可删。
    """
    drop_relative = [
        "tools/asr",
        "tools/uvr5",
        "docs",
        "Docker",
        "Dockerfile",
        "docker-compose.yaml",
        "install.ps1",
        "install.sh",
        "go-webui.bat",
        "go-webui.ps1",
        ".github",
        "GPT_weights",
        "SoVITS_weights",
        "TEMP",
        "logs",
        "output",
    ]
    for rel in drop_relative:
        p = repo_root / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            logger.info("strip dir: %s", rel)
        elif p.exists():
            p.unlink(missing_ok=True)
            logger.info("strip file: %s", rel)


def _download_base_models(python_exe: Path, base_models_dir: Path, work_dir: Path) -> None:
    """用 huggingface_hub.snapshot_download 拉基础模型。

    huggingface_hub 装在 stage 的 embeddable python 中（与运行时 deps 共用），
    通过子进程调用避免与构建机器 host python 耦合。
    下载完后做 fail-fast 校验，文件/目录缺失立刻 SystemExit。
    """
    base_models_dir.mkdir(parents=True, exist_ok=True)
    # 用 stage python 跑下载脚本（这样 huggingface_hub 装哪都行；只要 stage python 能找到）
    snippet = (
        "import sys\n"
        "from huggingface_hub import snapshot_download\n"
        "patterns = " + repr(_HF_BASE_MODELS_PATTERNS) + "\n"
        "snapshot_download(repo_id=" + repr(_HF_BASE_MODELS_REPO) + ", "
        "local_dir=" + repr(str(base_models_dir)) + ", "
        "allow_patterns=patterns, max_workers=4)\n"
        "print('hf snapshot_download done')\n"
    )
    script = work_dir / "_hf_download.py"
    script.write_text(snippet, encoding="utf-8")
    logger.info(
        "huggingface snapshot_download: repo=%s patterns=%s",
        _HF_BASE_MODELS_REPO, _HF_BASE_MODELS_PATTERNS,
    )
    subprocess.run([str(python_exe), str(script)], check=True)
    script.unlink(missing_ok=True)

    _verify_base_models(base_models_dir)


def _verify_base_models(base_models_dir: Path) -> None:
    """对下载的 base_models 做 fail-fast 校验。任何缺失抛 SystemExit。"""
    missing_files: list[str] = []
    for rel in _BASE_MODELS_REQUIRED_FILES:
        p = base_models_dir / rel
        if not p.exists():
            missing_files.append(rel)
        elif p.stat().st_size == 0:
            missing_files.append(f"{rel} (0 bytes)")
    empty_dirs: list[str] = []
    for rel in _BASE_MODELS_REQUIRED_NONEMPTY_DIRS:
        p = base_models_dir / rel
        if not p.is_dir() or not any(p.iterdir()):
            empty_dirs.append(rel)

    if missing_files or empty_dirs:
        lines = ["base_models 校验失败：HuggingFace 下载结果不完整。"]
        if missing_files:
            lines.append("  缺文件：")
            lines += [f"    - {f}" for f in missing_files]
        if empty_dirs:
            lines.append("  缺/空目录：")
            lines += [f"    - {d}" for d in empty_dirs]
        lines.append(
            f"  请检查 HF repo {_HF_BASE_MODELS_REPO} 的实际目录树是否还匹配"
            f" _HF_BASE_MODELS_PATTERNS={_HF_BASE_MODELS_PATTERNS}"
        )
        raise SystemExit("\n".join(lines))
    logger.info(
        "base_models 校验通过：%d 个必需文件 + %d 个必需目录 OK",
        len(_BASE_MODELS_REQUIRED_FILES),
        len(_BASE_MODELS_REQUIRED_NONEMPTY_DIRS),
    )


def _make_zip(src_dir: Path, out_zip: Path) -> None:
    """打包整个 src_dir 为 out_zip；zip 内路径以 src_dir 下相对路径计。"""
    logger.info("zip %s → %s", src_dir, out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in src_dir.rglob("*"):
            if path.is_dir():
                continue
            arcname = path.relative_to(src_dir)
            zf.write(path, arcname.as_posix())


def _upload_to_huggingface(
    zip_path: Path, hf_repo: str, path_in_repo: str | None = None, commit_message: str | None = None
) -> str:
    """上传 zip 到 HuggingFace Hub；返回可直接下载的 resolve URL。

    要求：HF_TOKEN 环境变量已设置（write 权限）。
    HF 公开仓库单文件上限 50GB，远超 GitHub Release 2GB；速度通常比 GitHub 稳。
    """
    import os
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit(
            "HF_TOKEN 环境变量未设置；HuggingFace 上传需要 write 权限的 token。"
            "本地测试可去 https://huggingface.co/settings/tokens 创建。"
        )
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise SystemExit(
            f"未安装 huggingface_hub: {e}；请 `uv sync` 或 `pip install huggingface_hub>=0.20`"
        ) from e

    if path_in_repo is None:
        path_in_repo = zip_path.name
    msg = commit_message or f"upload runtime: {zip_path.name}"
    size_mb = zip_path.stat().st_size / 1024 / 1024
    logger.info(
        "uploading to huggingface: repo=%s path=%s size=%.1fMB",
        hf_repo, path_in_repo, size_mb,
    )
    api = HfApi(token=token)
    api.upload_file(
        path_or_fileobj=str(zip_path),
        path_in_repo=path_in_repo,
        repo_id=hf_repo,
        repo_type="model",
        commit_message=msg,
    )
    # HF resolve URL：https://huggingface.co/<repo>/resolve/main/<path>
    # 不带 `?download=true` 也可，但带的话避免某些客户端拿到 HTML 重定向页
    url = f"https://huggingface.co/{hf_repo}/resolve/main/{path_in_repo}"
    logger.info("upload done: %s", url)
    return url


def build(version: str, python_version: str, profile: str, out_dir: Path) -> Path:
    if sys.platform != "win32":
        raise SystemExit(
            f"build_runtime.py 仅支持 Windows（embeddable 是 Windows 专属），当前 {sys.platform}"
        )
    if not _SERVE_PY.exists():
        raise SystemExit(f"serve.py 不存在：{_SERVE_PY}")
    if profile not in ("minimal", "full"):
        raise SystemExit(f"invalid profile: {profile}")

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="local-tts-runtime-") as tmp:
        tmp_path = Path(tmp)
        stage = tmp_path / "stage"
        stage.mkdir()

        # 1. 下载 + 解压 embeddable Python
        embed_url = (
            f"https://www.python.org/ftp/python/{python_version}/"
            f"python-{python_version}-embed-amd64.zip"
        )
        embed_zip = tmp_path / "python-embed.zip"
        _download(embed_url, embed_zip)
        python_dir = stage / "python"
        _extract_zip(embed_zip, python_dir)

        # 2. 装 pip
        python_exe = _bootstrap_pip(python_dir)

        # 3. 装最小依赖
        _pip_install(python_exe, _MINIMAL_DEPS)

        if profile == "full":
            # 4. CPU torch + serve.py 推理依赖（numpy / soundfile / matplotlib）
            #   显式 CPU index，避免拉到 CUDA 大轮子超 2GB Release 限制
            _pip_install(
                python_exe,
                _TORCH_DEPS,
                extra_index="https://download.pytorch.org/whl/cpu",
            )
            _pip_install(python_exe, _SERVE_PY_INFER_DEPS)

            # 5. 克隆 GPT-SoVITS 源码到 stage 根（serve.py 用 sys.path.insert(runtime_root)）
            gpt_sovits_clone = stage / "_gpt_sovits_clone"
            _clone_gpt_sovits(gpt_sovits_clone)
            # 仅保留 GPT_SoVITS/ 与 tools/（推理需要）
            src_pkg = gpt_sovits_clone / "GPT_SoVITS"
            if not src_pkg.exists():
                raise SystemExit(f"GPT_SoVITS/ 子目录缺失，仓库结构变了？检查 {gpt_sovits_clone}")
            shutil.move(str(src_pkg), str(stage / "GPT_SoVITS"))
            src_tools = gpt_sovits_clone / "tools"
            if src_tools.exists():
                shutil.move(str(src_tools), str(stage / "tools"))
            # requirements.txt 留出来给下一步用
            req_file = gpt_sovits_clone / "requirements.txt"
            if req_file.exists():
                shutil.move(str(req_file), str(tmp_path / "gpt_sovits_requirements.txt"))
            # 删剩余文件
            shutil.rmtree(gpt_sovits_clone, ignore_errors=True)
            _strip_gpt_sovits_extras(stage)

            # 6. 装 GPT-SoVITS 完整运行依赖（librosa/transformers/...）
            #   先过滤掉 pyopenjtalk(JA, 需 CMake)/jieba_fast(C ext)/gradio(webui)/faster-whisper(ASR) 等
            #   再装 cmake/setuptools/wheel 作为剩余 C 扩展包的兜底构建工具
            req_path = tmp_path / "gpt_sovits_requirements.txt"
            if req_path.exists():
                filtered_req = tmp_path / "gpt_sovits_requirements_filtered.txt"
                dropped = _filter_requirements(req_path, filtered_req, _DROP_REQUIREMENTS)
                for line in dropped:
                    logger.warning("dropped requirement: %s", line)

                # 装编译工具（safety net；GPT-SoVITS 仍可能拉 C 扩展包）
                _pip_install(python_exe, ["cmake", "setuptools>=68", "wheel"])

                _pip_install_requirements(python_exe, filtered_req)

                # opencc 被从 GPT-SoVITS 依赖中过滤掉（C++ binding，嵌入式 Python 无法构建）。
                # 用纯 Python 的 opencc-python-reimplemented 替代，API 兼容 `from opencc import OpenCC`。
                _pip_install(python_exe, ["opencc-python-reimplemented"])

                # 给被 drop 的包建 stub（GPT_SoVITS 可能在模块级 import 它们）
                _create_stub_packages(python_dir, _STUB_PACKAGES)
            else:
                logger.warning("GPT-SoVITS requirements.txt 未找到，跳过深依赖安装")

            # 7. 装 huggingface_hub 用于拉基础模型；基础模型放 stage/base_models/
            _pip_install(python_exe, _HF_DOWNLOAD_DEPS)
            base_models = stage / "base_models"
            _download_base_models(python_exe, base_models, tmp_path)

        # 8. 复制 serve.py 到 stage 根
        shutil.copy2(_SERVE_PY, stage / "serve.py")

        # 9. 写 VERSION
        (stage / "VERSION").write_text(version, encoding="utf-8")

        # 10. zip
        out_zip = out_dir / f"local-tts-runtime-v{version}.zip"
        if out_zip.exists():
            out_zip.unlink()
        _make_zip(stage, out_zip)

        size_mb = out_zip.stat().st_size / 1024 / 1024
        logger.info("done: %s (%.1f MB) profile=%s", out_zip, size_mb, profile)
        if size_mb > 2000:
            logger.info(
                "zip > 2GB —— 已超 GitHub Release 单文件上限，必须用 --hf-repo 上传到 HuggingFace"
                "（HF 单文件 50GB 上限够用）"
            )
        return out_zip


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="Build local TTS runtime zip for Windows")
    parser.add_argument("--version", default="0.1.0", help="VERSION 文件内容 + zip 文件名版本")
    parser.add_argument(
        "--python-version",
        default="3.10.11",
        help=(
            "Python embeddable 版本（需与 python.org/ftp/python 上 -embed-amd64.zip 对应）。"
            "默认 3.10.11：GPT-SoVITS requirements.txt pin 了 numba==0.56.4，"
            "该版本只支持 Python 3.7-3.10；3.11+ 装不上。"
            "升级 Python 须同时升级 numba pin（要等 GPT-SoVITS 上游解锁）。"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["minimal", "full"],
        default="full",
        help="minimal=仅 serve.py 框架(≈50MB,仅 --mock 可用) / full=含 torch+GPT-SoVITS+基础模型(≈1.8GB)",
    )
    parser.add_argument("--out", default="dist", help="输出目录")
    parser.add_argument(
        "--hf-repo",
        default="",
        help=(
            "HuggingFace 上传目标仓库 ID（如 woaye168/bgd-worker-npc-voice-gen-runtime）。"
            "空值 = 不上传（仅本地构建用）；非空时要求 HF_TOKEN 环境变量含 write 权限 token。"
            "构建完会上传 zip 到该仓库的 main 分支，stdout 打印 HF_DOWNLOAD_URL 行供 CI 抓。"
        ),
    )
    parser.add_argument(
        "--hf-path-in-repo",
        default="",
        help="上传到 HF 仓库内的路径；默认用 zip 文件名（如 local-tts-runtime-v0.2.0.zip）",
    )
    args = parser.parse_args(argv)

    out = build(
        version=args.version,
        python_version=args.python_version,
        profile=args.profile,
        out_dir=Path(args.out),
    )
    print(f"OUTPUT={out}")

    if args.hf_repo:
        hf_url = _upload_to_huggingface(
            zip_path=out,
            hf_repo=args.hf_repo,
            path_in_repo=args.hf_path_in_repo or None,
            commit_message=f"runtime build v{args.version}",
        )
        # CI 抓这行回填到 catalog.json 的 download_url
        print(f"HF_DOWNLOAD_URL={hf_url}")
    else:
        logger.info("跳过 HF 上传（--hf-repo 未指定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
