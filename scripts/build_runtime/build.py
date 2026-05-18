# @purpose: build_runtime.py 的构建编排、zip 打包、HF 上传、CLI 入口
# @layer: build-tool
# @contract:
#   - build(version, python_version, profile, out_dir, target) -> Path
#   - _make_zip(src_dir, out_zip)
#   - _upload_to_huggingface(zip_path, hf_repo, ...) -> str
#   - main(argv) -> int
# @invariants:
#   - 仅 Windows runners
#   - VERSION 文件格式 `<ver> <target>`（如 "0.3.0 amd-rocm"）
#   - zip 文件名 local-tts-runtime-<target>-v<ver>.zip 与 catalog.json 对齐
#   - build() 输出 catalog 片段（JSON per-target slot 含 version/url/sha256/size_bytes）
#     供 CI/GH Action 直接抓回 catalog.json

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .constants import (
    _HF_DOWNLOAD_DEPS,
    _MINIMAL_DEPS,
    _SERVE_PY,
    _SERVE_PY_INFER_DEPS,
    _STUB_PACKAGES,
    _VALID_TARGETS,
)
from .gpt_sovits import (
    _clone_gpt_sovits,
    _download_base_models,
    _download_fast_langdetect_model,
    _download_g2pw_model,
    _mirror_v4_pretrained_for_hardcoded_paths,
    _patch_gpt_sovits_tts_dtype,
    _patch_tools_my_utils_ffmpeg,
    _strip_gpt_sovits_extras,
)
from .packaging import (
    _bootstrap_pip,
    _create_stub_packages,
    _download,
    _extract_zip,
    _filter_requirements,
    _install_torch_for_target,
    _pip_install,
    _pip_install_requirements,
)

logger = logging.getLogger("build_runtime")


def _strip_ort_cuda_provider(python_dir: Path) -> int:
    """删除 onnxruntime 的 CUDA EP DLL（cpu / amd-rocm runtime 不需要）。

    背景：onnxruntime（GPT-SoVITS G2PW 多音字消解依赖）默认 providers 顺序
    [CUDAExecutionProvider, CPUExecutionProvider]。CUDA EP DLL 存在但
    cublasLt64_12.dll 缺失时（amd-rocm / cpu 包都没装 CUDA runtime） →
    抛 "Error loading ... cublasLt64_12.dll missing" 错误日志，干扰用户排查。

    干脆删 onnxruntime_providers_cuda.dll：ORT 找不到该 EP 不会尝试加载，
    日志干净 + 启动略快。nvidia-cuda 包保留（用户有 NV CUDA runtime，能加载成功）。

    返回删除的文件数；找不到则返回 0（不抛错）。
    """
    site_packages = python_dir / "Lib" / "site-packages"
    ort_capi = site_packages / "onnxruntime" / "capi"
    if not ort_capi.exists():
        return 0
    patterns = ("onnxruntime_providers_cuda*.dll", "onnxruntime_providers_tensorrt*.dll")
    removed = 0
    for pat in patterns:
        for p in ort_capi.glob(pat):
            try:
                p.unlink()
                removed += 1
                logger.info("strip ORT EP: %s", p.relative_to(site_packages))
            except Exception as e:
                logger.warning("strip ORT EP failed: %s (%s)", p, e)
    if removed:
        logger.info("stripped %d ORT CUDA/TRT EP DLLs from amd-rocm/cpu runtime", removed)
    else:
        logger.info("no ORT CUDA EP DLL found to strip (probably onnxruntime CPU-only wheel)")
    return removed


def _make_zip(src_dir: Path, out_zip: Path) -> None:
    logger.info("zip %s -> %s", src_dir, out_zip)
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
    url = f"https://huggingface.co/{hf_repo}/resolve/main/{path_in_repo}"
    logger.info("upload done: %s", url)
    return url


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build(
    version: str,
    python_version: str,
    profile: str,
    out_dir: Path,
    target: str = "cpu",
) -> Path:
    if sys.platform != "win32":
        raise SystemExit(
            f"scripts/build_runtime/ 仅支持 Windows（embeddable 是 Windows 专属），当前 {sys.platform}"
        )
    if not _SERVE_PY.exists():
        raise SystemExit(f"serve.py 不存在：{_SERVE_PY}")
    if profile not in ("minimal", "full"):
        raise SystemExit(f"invalid profile: {profile}")
    if target not in _VALID_TARGETS:
        raise SystemExit(f"invalid target: {target}; valid: {_VALID_TARGETS}")

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
            # 4. torch（按 target 选 wheel 源）+ serve.py 推理依赖
            _install_torch_for_target(python_exe, target)
            _pip_install(python_exe, _SERVE_PY_INFER_DEPS)

            # 5. 克隆 GPT-SoVITS 源码
            gpt_sovits_clone = stage / "_gpt_sovits_clone"
            _clone_gpt_sovits(gpt_sovits_clone)
            src_pkg = gpt_sovits_clone / "GPT_SoVITS"
            if not src_pkg.exists():
                raise SystemExit(f"GPT_SoVITS/ 子目录缺失，仓库结构变了？检查 {gpt_sovits_clone}")
            shutil.move(str(src_pkg), str(stage / "GPT_SoVITS"))
            src_tools = gpt_sovits_clone / "tools"
            if src_tools.exists():
                shutil.move(str(src_tools), str(stage / "tools"))
            req_file = gpt_sovits_clone / "requirements.txt"
            if req_file.exists():
                shutil.move(str(req_file), str(tmp_path / "gpt_sovits_requirements.txt"))
            shutil.rmtree(gpt_sovits_clone, ignore_errors=True)
            _strip_gpt_sovits_extras(stage)
            _patch_gpt_sovits_tts_dtype(stage)
            _patch_tools_my_utils_ffmpeg(stage)

            # 6. 装 GPT-SoVITS 完整运行依赖
            req_path = tmp_path / "gpt_sovits_requirements.txt"
            if req_path.exists():
                filtered_req = tmp_path / "gpt_sovits_requirements_filtered.txt"
                dropped = _filter_requirements(req_path, filtered_req, _STUB_PACKAGES.keys())
                for line in dropped:
                    logger.warning("dropped requirement: %s", line)
                _pip_install(python_exe, ["cmake", "setuptools>=68", "wheel"])
                _pip_install_requirements(python_exe, filtered_req)
                _pip_install(python_exe, ["opencc-python-reimplemented"])
                _create_stub_packages(python_dir, _STUB_PACKAGES)
            else:
                logger.warning("GPT-SoVITS requirements.txt 未找到，跳过深依赖安装")

            # 7. 装 huggingface_hub 用于拉基础模型
            _pip_install(python_exe, _HF_DOWNLOAD_DEPS)
            base_models = stage / "base_models"
            _download_base_models(python_exe, base_models, tmp_path)
            _mirror_v4_pretrained_for_hardcoded_paths(stage, base_models)
            _download_fast_langdetect_model(stage)
            _download_g2pw_model(stage)

            # 7.5 非 nvidia-cuda 包：清掉 onnxruntime 的 CUDA EP DLL
            # 否则启动时 ORT 会试图加载 CUDA EP（cublasLt64_12.dll missing → 错误日志噪声 +
            # 启动稍慢）。CPU EP 处理 GPT-SoVITS 的 G2PW（多音字 ONNX 模型）已够用。
            if target != "nvidia-cuda":
                _strip_ort_cuda_provider(python_dir)

        # 8. 复制 serve.py 到 stage 根
        shutil.copy2(_SERVE_PY, stage / "serve.py")

        # 9. 写 VERSION：`<ver> <target>` 格式
        (stage / "VERSION").write_text(f"{version} {target}", encoding="utf-8")

        # 10. zip
        out_zip = out_dir / f"local-tts-runtime-{target}-v{version}.zip"
        if out_zip.exists():
            out_zip.unlink()
        _make_zip(stage, out_zip)

        size_mb = out_zip.stat().st_size / 1024 / 1024
        sha = _sha256_file(out_zip)
        size_bytes = out_zip.stat().st_size
        logger.info("done: %s (%.1f MB) profile=%s target=%s", out_zip, size_mb, profile, target)
        if size_mb > 2000:
            logger.info(
                "zip > 2GB -- 已超 GitHub Release 单文件上限，必须用 --hf-repo 上传到 HuggingFace"
                "（HF 单文件 50GB 上限够用）"
            )

        # 输出 catalog 片段（新 schema v2：per-target version），供 CI 抓回 catalog.json
        slot = {
            "version": version,
            "download_url": f"https://huggingface.co/woaye168/bgd-worker-npc-voice-gen-runtime/resolve/main/{out_zip.name}",
            "size_bytes": size_bytes,
            "sha256": sha,
        }
        print(f"CATALOG_SLOT={json.dumps(slot, ensure_ascii=False)}")
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
        help="Python embeddable 版本（默认 3.10.11）",
    )
    parser.add_argument(
        "--profile",
        choices=["minimal", "full"],
        default="full",
        help="minimal=仅 serve.py 框架(≈50MB) / full=含 torch+GPT-SoVITS+基础模型",
    )
    parser.add_argument(
        "--target",
        choices=list(_VALID_TARGETS),
        default="cpu",
        help="推理后端 target",
    )
    parser.add_argument("--out", default="dist", help="输出目录")
    parser.add_argument(
        "--hf-repo",
        default="",
        help="HuggingFace 上传目标仓库 ID（空值=不上传）",
    )
    parser.add_argument(
        "--hf-path-in-repo",
        default="",
        help="上传到 HF 仓库内的路径；默认用 zip 文件名",
    )
    args = parser.parse_args(argv)

    out = build(
        version=args.version,
        python_version=args.python_version,
        profile=args.profile,
        out_dir=Path(args.out),
        target=args.target,
    )
    print(f"OUTPUT={out}")
    print(f"TARGET={args.target}")

    if args.hf_repo:
        hf_url = _upload_to_huggingface(
            zip_path=out,
            hf_repo=args.hf_repo,
            path_in_repo=args.hf_path_in_repo or None,
            commit_message=f"runtime build v{args.version}",
        )
        print(f"HF_DOWNLOAD_URL={hf_url}")
    else:
        logger.info("跳过 HF 上传（--hf-repo 未指定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
