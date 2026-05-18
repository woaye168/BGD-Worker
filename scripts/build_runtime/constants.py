# @purpose: build_runtime.py 的常量与配置（依赖列表、过滤规则、torch 索引）
# @layer: build-tool
# @invariants:
#   - 仅 Windows runners：上层脚本断言 sys.platform == 'win32'
#   - torch 安装由 _TORCH_DEPS_BY_TARGET 按 target 选择源：
#     cpu→PyPI 默认 cpu wheel；amd-rocm→ROCm nightly gfx1151；nvidia-cuda→cu126
#   - profile=minimal：仅 fastapi+uvicorn+pydantic+serve.py（≈50MB；target 无效）
#   - profile=full（默认）：再加 torch(target wheel) + 完整 GPT-SoVITS 源码 + V4 基础模型
#   - 产物 zip 文件名 local-tts-runtime-<target>-v<ver>.zip 与 catalog.json windows_x64_<target> 段对齐

from pathlib import Path

# 仓库根目录（scripts/ 的父目录）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVE_PY = _REPO_ROOT / "tts" / "runtime" / "serve.py"

# 最小依赖：FastAPI + uvicorn + pydantic（serve.py 直接需要）
_MINIMAL_DEPS = [
    "fastapi>=0.115",
    "uvicorn>=0.32",
    "pydantic>=2.9",
]

# Full profile 额外依赖：serve.py 推理流程需要 numpy + soundfile
# GPT-SoVITS 的其余深依赖走 pip install -r GPT_SoVITS/requirements.txt
# matplotlib / nltk / pandas 均不在 GPT-SoVITS requirements.txt 中，但推理路径在模块级引用
_SERVE_PY_INFER_DEPS = ["numpy", "soundfile", "matplotlib", "nltk", "pandas"]

_TORCH_PACKAGES = ["torch", "torchaudio"]
_AUX_TORCH_DEPS = ["imageio-ffmpeg"]

_TORCH_INDEX_BY_TARGET = {
    "cpu": ("https://download.pytorch.org/whl/cpu", "extra"),
    "amd-rocm": ("https://rocm.nightlies.amd.com/v2/gfx1151/", "primary-pre"),
    "nvidia-cuda": ("https://download.pytorch.org/whl/cu126", "extra"),
}
_VALID_TARGETS = tuple(_TORCH_INDEX_BY_TARGET.keys())

_HF_DOWNLOAD_DEPS = ["huggingface_hub"]

_GPT_SOVITS_REPO = "https://github.com/RVC-Boss/GPT-SoVITS.git"
_GPT_SOVITS_REF = "20250422v4"
_HF_BASE_MODELS_REPO = "lj1995/GPT-SoVITS"
_HF_BASE_MODELS_PATTERNS = [
    "chinese-roberta-wwm-ext-large/**",
    "chinese-hubert-base/**",
    "s1v3.ckpt",
    "gsv-v4-pretrained/**",
]

_BASE_MODELS_REQUIRED_FILES = [
    "s1v3.ckpt",
    "gsv-v4-pretrained/s2Gv4.pth",
]
_BASE_MODELS_REQUIRED_NONEMPTY_DIRS = [
    "chinese-roberta-wwm-ext-large",
    "chinese-hubert-base",
    "gsv-v4-pretrained",
]

_DROP_REQUIREMENTS = {
    "pyopenjtalk",
    "pyopenjtalk-prebuilt",
    "jieba_fast",
    "opencc",
    "gradio",
    "gradio_client",
    "faster-whisper",
    "funasr",
    "modelscope",
    "wetextprocessing",
    "pynini",
    "nemo-text-processing",
    "nemo_text_processing",
}

_STUB_PACKAGES = {
    "pyopenjtalk": {
        "type": "error",
        "attrs": ["g2p", "run_frontend", "extract_fullcontext", "make_label", "load_marine_model"],
        "error_msg": "pyopenjtalk 未打包到此 runtime（仅中文/英文模型可用）",
    },
    "jieba_fast": {
        "type": "alias",
        "alias_of": "jieba",
    },
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
