# @purpose: build_runtime.py 的 GPT-SoVITS 源码克隆、裁剪、补丁、模型下载
# @layer: build-tool
# @contract:
#   - _clone_gpt_sovits(target_dir)
#   - _strip_gpt_sovits_extras(repo_root)
#   - _patch_gpt_sovits_tts_dtype(stage)
#   - _patch_tools_my_utils_ffmpeg(stage)
#   - _download_fast_langdetect_model(stage)
#   - _download_g2pw_model(stage)
#   - _download_base_models(python_exe, base_models_dir, work_dir)
#   - _verify_base_models(base_models_dir)
#   - _mirror_v4_pretrained_for_hardcoded_paths(stage, base_models)
# @invariants:
#   - V4 配置（s1v3.ckpt + gsv-v4-pretrained/s2Gv4.pth）与 serve.py 对齐
#   - tools/i18n 与 tools/AP_BWE_main 不可删（推理路径在模块级引用）
#   - V4 上游硬编码 vocoder 路径 → 必须镜像 base_models/gsv-v4-pretrained/ 到
#     GPT_SoVITS/pretrained_models/gsv-v4-pretrained/
#   - base_models 下载后做 fail-fast 校验

import logging
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .constants import (
    _BASE_MODELS_REQUIRED_FILES,
    _BASE_MODELS_REQUIRED_NONEMPTY_DIRS,
    _GPT_SOVITS_REF,
    _GPT_SOVITS_REPO,
    _HF_BASE_MODELS_PATTERNS,
    _HF_BASE_MODELS_REPO,
)

logger = logging.getLogger("build_runtime")


def _clone_gpt_sovits(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth=1", "--branch", _GPT_SOVITS_REF, _GPT_SOVITS_REPO, str(target)]
    logger.info("git clone: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    git_dir = target / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)


def _strip_gpt_sovits_extras(repo_root: Path) -> None:
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


def _patch_gpt_sovits_tts_dtype(stage: Path) -> None:
    tts_py = stage / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py"
    if not tts_py.exists():
        logger.warning("TTS.py not found, skip dtype patch")
        return

    text = tts_py.read_text(encoding="utf-8")
    patched = False

    old_cnhubert = (
        "        if self.configs.is_half and str(self.configs.device) != \"cpu\":\n"
        "            self.cnhuhbert_model = self.cnhuhbert_model.half()\n"
    )
    new_cnhubert = (
        "        if self.configs.is_half and str(self.configs.device) != \"cpu\":\n"
        "            self.cnhuhbert_model = self.cnhuhbert_model.half()\n"
        "        else:\n"
        "            self.cnhuhbert_model = self.cnhuhbert_model.float()\n"
    )
    if old_cnhubert in text:
        text = text.replace(old_cnhubert, new_cnhubert)
        patched = True
        logger.info("patched init_cnhuhbert_weights: add .float() fallback")
    else:
        logger.warning("init_cnhuhbert_weights patch pattern not found")

    old_bert = (
        "        if self.configs.is_half and str(self.configs.device) != \"cpu\":\n"
        "            self.bert_model = self.bert_model.half()\n"
    )
    new_bert = (
        "        if self.configs.is_half and str(self.configs.device) != \"cpu\":\n"
        "            self.bert_model = self.bert_model.half()\n"
        "        else:\n"
        "            self.bert_model = self.bert_model.float()\n"
    )
    if old_bert in text:
        text = text.replace(old_bert, new_bert)
        patched = True
        logger.info("patched init_bert_weights: add .float() fallback")
    else:
        logger.warning("init_bert_weights patch pattern not found")

    if patched:
        tts_py.write_text(text, encoding="utf-8")


def _patch_tools_my_utils_ffmpeg(stage: Path) -> None:
    my_utils = stage / "tools" / "my_utils.py"
    if not my_utils.exists():
        logger.warning("tools/my_utils.py not found, skip ffmpeg patch")
        return

    text = my_utils.read_text(encoding="utf-8")
    old_import = "import pandas as pd\n"
    new_import = (
        "import pandas as pd\n\n"
        "try:\n"
        "    import imageio_ffmpeg\n"
        "    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()\n"
        "except Exception:\n"
        "    _FFMPEG_EXE = \"ffmpeg\"\n"
    )
    if old_import in text and "_FFMPEG_EXE" not in text:
        text = text.replace(old_import, new_import, 1)
    else:
        logger.warning("tools/my_utils.py import patch pattern not found")
        return

    old_run = '.run(cmd=["ffmpeg", "-nostdin"], capture_stdout=True, capture_stderr=True)'
    new_run = '.run(cmd=[_FFMPEG_EXE, "-nostdin"], capture_stdout=True, capture_stderr=True)'
    if old_run in text:
        text = text.replace(old_run, new_run, 1)
        logger.info("patched tools/my_utils.py: use imageio_ffmpeg absolute path")
    else:
        logger.warning("tools/my_utils.py ffmpeg cmd patch pattern not found")

    my_utils.write_text(text, encoding="utf-8")


def _download_fast_langdetect_model(stage: Path) -> None:
    target_dir = stage / "GPT_SoVITS" / "pretrained_models" / "fast_langdetect"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "lid.176.bin"
    if target_file.exists() and target_file.stat().st_size > 0:
        logger.info("fast_langdetect model already cached: %s", target_file)
        return

    url = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
    logger.info("downloading fast_langdetect model: %s -> %s", url, target_file)
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            with target_file.open("wb") as f:
                shutil.copyfileobj(resp, f)
        size_mb = target_file.stat().st_size / 1024 / 1024
        logger.info("fast_langdetect model downloaded: %.1f MB", size_mb)
    except Exception:
        logger.exception("fast_langdetect model download failed")
        raise SystemExit(
            "fast_langdetect 模型下载失败（需要外网访问 Facebook CDN）。"
            "检查网络或代理后重试。"
        )


def _download_g2pw_model(stage: Path) -> None:
    target_dir = stage / "GPT_SoVITS" / "text" / "G2PWModel"
    if target_dir.exists() and any(target_dir.iterdir()):
        logger.info("G2PW model already present: %s", target_dir)
        return

    zip_url = (
        "https://www.modelscope.cn/models/kamiorinn/g2pw/"
        "resolve/master/G2PWModel_1.1.zip"
    )
    with tempfile.TemporaryDirectory(prefix="g2pw-dl-") as tmp:
        tmp_path = Path(tmp)
        zip_file = tmp_path / "G2PWModel_1.1.zip"
        logger.info("downloading G2PW model: %s -> %s", zip_url, zip_file)
        try:
            with urllib.request.urlopen(zip_url, timeout=300) as resp:
                with zip_file.open("wb") as f:
                    shutil.copyfileobj(resp, f)
        except Exception:
            logger.exception("G2PW model download failed")
            raise SystemExit(
                "G2PW 模型下载失败（需要外网访问 ModelScope CDN）。"
                "检查网络或代理后重试。"
            )

        logger.info("extracting G2PW model ...")
        with zipfile.ZipFile(zip_file) as zf:
            zf.extractall(tmp_path)
        extracted = tmp_path / "G2PWModel_1.1"
        if not extracted.exists():
            raise SystemExit(
                "G2PW 模型解压后目录 G2PWModel_1.1 不存在，"
                "可能是上游压缩包结构变化。"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in extracted.iterdir():
            dst = target_dir / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dst), dirs_exist_ok=True)
            else:
                shutil.copy2(str(item), str(dst))
        logger.info("G2PW model ready at %s", target_dir)


def _download_base_models(python_exe: Path, base_models_dir: Path, work_dir: Path) -> None:
    base_models_dir.mkdir(parents=True, exist_ok=True)
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


def _mirror_v4_pretrained_for_hardcoded_paths(stage: Path, base_models: Path) -> None:
    src = base_models / "gsv-v4-pretrained"
    if not src.exists():
        logger.warning("base_models/gsv-v4-pretrained 不存在，跳过镜像")
        return
    dst = stage / "GPT_SoVITS" / "pretrained_models" / "gsv-v4-pretrained"
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in src.iterdir():
        if f.is_file():
            dst_file = dst / f.name
            if not dst_file.exists():
                shutil.copy2(str(f), str(dst_file))
                copied += 1
    if copied:
        logger.info(
            "mirrored %d file(s) from base_models/gsv-v4-pretrained → "
            "GPT_SoVITS/pretrained_models/gsv-v4-pretrained (for upstream hardcoded paths)",
            copied,
        )
    else:
        logger.info("GPT_SoVITS/pretrained_models/gsv-v4-pretrained already up-to-date")
