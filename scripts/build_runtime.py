# @purpose: 本地 TTS 运行时打包脚本（Windows-only；CI 由 release.yml 触发）
# @layer: build-tool (非 app 一部分)
# @usage:
#   python scripts/build_runtime.py [--version 1.0.0] [--python-version 3.10.11] [--out dist]
# @output:
#   dist/local-tts-runtime-v<version>.zip
# @invariants:
#   - 仅 Windows runners：脚本断言 sys.platform == 'win32'，否则退出非 0
#   - 内置最小运行时（fastapi + uvicorn + pydantic + 我们的 serve.py）；torch + GPT-SoVITS 由
#     follow-up PR 接入（当前 zip 仅支持 --mock 模式，足以验证 IPC 链路 + CI 工作流）
#   - 产物结构与 LocalTTSRuntimeInstaller._extract_zip 期望一致：
#       <zip-root>/
#         VERSION
#         serve.py
#         python/python.exe + Lib/site-packages/...
#   - VERSION 文件内容 = --version 参数；LocalTTSRuntimeInstaller 据此显示 installed_version

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

_MINIMAL_DEPS = [
    "fastapi>=0.115",
    "uvicorn>=0.32",
    "pydantic>=2.9",
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


def _pip_install(python_exe: Path, packages: list[str]) -> None:
    logger.info("pip install: %s", " ".join(packages))
    subprocess.run(
        [str(python_exe), "-m", "pip", "install", "--no-warn-script-location", *packages],
        check=True,
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


def build(version: str, python_version: str, out_dir: Path) -> Path:
    if sys.platform != "win32":
        raise SystemExit(
            f"build_runtime.py 仅支持 Windows（embeddable 是 Windows 专属），当前 {sys.platform}"
        )
    if not _SERVE_PY.exists():
        raise SystemExit(f"serve.py 不存在：{_SERVE_PY}")

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="local-tts-runtime-") as tmp:
        tmp_path = Path(tmp)
        # stage 目录：将作为 zip 根；命名为 runtime/ 仅为可读
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

        # 4. 复制 serve.py 到 stage 根
        shutil.copy2(_SERVE_PY, stage / "serve.py")

        # 5. 写 VERSION
        (stage / "VERSION").write_text(version, encoding="utf-8")

        # 6. zip
        out_zip = out_dir / f"local-tts-runtime-v{version}.zip"
        if out_zip.exists():
            out_zip.unlink()
        _make_zip(stage, out_zip)

        size_mb = out_zip.stat().st_size / 1024 / 1024
        logger.info("done: %s (%.1f MB)", out_zip, size_mb)
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
    parser.add_argument("--out", default="dist", help="输出目录")
    args = parser.parse_args(argv)

    out = build(
        version=args.version,
        python_version=args.python_version,
        out_dir=Path(args.out),
    )
    print(f"OUTPUT={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
