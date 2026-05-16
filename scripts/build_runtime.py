# @purpose: 本地 TTS 运行时打包脚本（Windows-only；CI 由 release.yml 触发）
# @layer: build-tool (非 app 一部分)
# @usage:
#   python scripts/build_runtime.py [--version 1.0.0] [--python-version 3.11.9]
#                                   [--profile {minimal,full}] [--out dist]
# @output:
#   dist/local-tts-runtime-v<version>.zip
# @invariants:
#   - 仅 Windows runners：脚本断言 sys.platform == 'win32'
#   - profile=minimal：仅 fastapi+uvicorn+pydantic+serve.py（≈50MB；只能 --mock 模式跑）
#   - profile=full（默认）：再加 torch(cpu) + 完整 GPT-SoVITS 源码 + 基础模型
#     （Bert+HuBERT+预训练 GPT/SoVITS；≈1.8-2GB；可真实推理）
#   - GitHub Release 单文件上限 2GB；当前选 CPU torch + 单语种基础模型，控制在 2GB 内
#     （NVIDIA 用户初期落 CPU 模式，等后续 CUDA 变体）
#   - 产物结构（profile=full）：
#       <zip-root>/
#         VERSION
#         serve.py
#         python/python.exe + Lib/site-packages/...
#         GPT_SoVITS/                    ← 从官方仓库克隆
#         base_models/
#           chinese-roberta-wwm-ext-large/
#           chinese-hubert-base/
#           s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt
#           s2G488k.pth
#   - VERSION 文件内容 = --version 参数；LocalTTSRuntimeInstaller 据此显示 installed_version
#   - 临时目录使用 tempfile.TemporaryDirectory：失败/中断自动清理

from __future__ import annotations

import argparse
import logging
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
# 走 `pip install -r GPT-SoVITS/requirements.txt`（克隆后即时安装）
_SERVE_PY_INFER_DEPS = ["numpy", "soundfile"]

# CPU torch wheel（Windows x64，约 250MB；不含 CUDA）
# 不指定具体版本：让 pip 解析最新稳定；如需固定可加 ==2.4.0
_TORCH_DEPS = ["torch", "torchaudio"]

# 下载基础模型的工具
_HF_DOWNLOAD_DEPS = ["huggingface_hub"]

# GPT-SoVITS 仓库 + 基础模型 HF repo
_GPT_SOVITS_REPO = "https://github.com/RVC-Boss/GPT-SoVITS.git"
_GPT_SOVITS_REF = "main"  # 后续可固定到具体 release tag 提高复现性
_HF_BASE_MODELS_REPO = "lj1995/GPT-SoVITS"
_HF_BASE_MODELS_PATTERNS = [
    "chinese-roberta-wwm-ext-large/**",
    "chinese-hubert-base/**",
    "s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt",
    "s2G488k.pth",
]


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
    """移除 GPT-SoVITS 仓库中训练/微调/前端相关、推理不需要的目录与文件，节省 zip 体积。"""
    # 推理只依赖 GPT_SoVITS/ 子目录与 tools/ 中部分工具；其余 webui/微调/训练数据可去
    drop_relative = [
        "tools/asr",
        "tools/uvr5",
        "tools/AP_BWE",
        "tools/AP_BWE_main",
        "tools/i18n",  # 多语言资源；若需可保留
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
    logger.info("huggingface snapshot_download: repo=%s patterns=%s", _HF_BASE_MODELS_REPO, _HF_BASE_MODELS_PATTERNS)
    subprocess.run([str(python_exe), str(script)], check=True)
    script.unlink(missing_ok=True)


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
            # 4. CPU torch + serve.py 推理依赖（numpy / soundfile）
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

            # 6. 装 GPT-SoVITS 完整运行依赖（librosa/transformers/jieba_fast/...）
            req_path = tmp_path / "gpt_sovits_requirements.txt"
            if req_path.exists():
                _pip_install_requirements(python_exe, req_path)
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
            logger.warning("zip > 2GB；GitHub Release 单文件上限 2GB，请考虑切回 minimal 或拆分")
        return out_zip


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="Build local TTS runtime zip for Windows")
    parser.add_argument("--version", default="0.1.0", help="VERSION 文件内容 + zip 文件名版本")
    parser.add_argument(
        "--python-version",
        default="3.11.9",
        help="Python embeddable 版本（需与 python.org/ftp/python 上 -embed-amd64.zip 对应）",
    )
    parser.add_argument(
        "--profile",
        choices=["minimal", "full"],
        default="full",
        help="minimal=仅 serve.py 框架(≈50MB,仅 --mock 可用) / full=含 torch+GPT-SoVITS+基础模型(≈1.8GB)",
    )
    parser.add_argument("--out", default="dist", help="输出目录")
    args = parser.parse_args(argv)

    out = build(
        version=args.version,
        python_version=args.python_version,
        profile=args.profile,
        out_dir=Path(args.out),
    )
    print(f"OUTPUT={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
