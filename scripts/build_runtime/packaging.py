# @purpose: scripts/build_runtime/ 的 Python embeddable / pip / torch 安装与通用工具
# @layer: build-tool
# @contract:
#   - _download(url, target_path)
#   - _extract_zip(zip_path, target_dir)
#   - _bootstrap_pip(python_dir) -> python_exe
#   - _pip_install(python_exe, packages, extra_index=, primary_index=, pre=)
#   - _install_torch_for_target(python_exe, target)
#   - _pip_install_requirements(python_exe, requirements_file)
#   - _filter_requirements(src, dst, drop_names) -> dropped_lines
#   - _site_packages_dir(python_dir) -> Path
#   - _create_stub_packages(python_dir, stubs)
# @invariants:
#   - 仅 Windows 嵌入式 Python；sys.platform != win32 在上层处理
#   - 嵌入式 Python pth 文件会被修改以启用 site 导入（pip 需要）
#   - _install_torch_for_target 按 target 选择 index-url/extra-index-url + --pre

import logging
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from .constants import (
    _AUX_TORCH_DEPS,
    _DROP_REQUIREMENTS,
    _STUB_PACKAGES,
    _TORCH_INDEX_BY_TARGET,
    _TORCH_PACKAGES,
    _VALID_TARGETS,
)

logger = logging.getLogger("build_runtime")


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
    """在 embeddable Python 中安装 pip；返回 python.exe 路径。"""
    python_exe = python_dir / "python.exe"
    if not python_exe.exists():
        raise RuntimeError(f"python.exe not found in {python_dir}")

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


def _pip_install(
    python_exe: Path,
    packages: list[str],
    *,
    extra_index: str | None = None,
    primary_index: str | None = None,
    pre: bool = False,
) -> None:
    args = [
        str(python_exe), "-m", "pip", "install",
        "--no-warn-script-location",
    ]
    if pre:
        args.append("--pre")
    if primary_index:
        args.extend(["--index-url", primary_index])
    if extra_index:
        args.extend(["--extra-index-url", extra_index])
    args.extend(packages)
    logger.info(
        "pip install: %s%s%s%s",
        " ".join(packages),
        f" --index-url {primary_index}" if primary_index else "",
        f" --extra-index-url {extra_index}" if extra_index else "",
        " --pre" if pre else "",
    )
    subprocess.run(args, check=True)


def _install_torch_for_target(python_exe: Path, target: str) -> None:
    if target not in _TORCH_INDEX_BY_TARGET:
        raise SystemExit(f"unknown target: {target}; valid: {_VALID_TARGETS}")
    index_url, kind = _TORCH_INDEX_BY_TARGET[target]
    if kind == "primary-pre":
        _pip_install(python_exe, _TORCH_PACKAGES, primary_index=index_url, pre=True)
    else:
        _pip_install(python_exe, _TORCH_PACKAGES, extra_index=index_url)
    _pip_install(python_exe, _AUX_TORCH_DEPS)


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
    drop_norm = {d.lower().replace("_", "-") for d in drop_names}
    dropped: list[str] = []
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for raw in fin:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                fout.write(raw)
                continue
            name_match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", stripped)
            if not name_match:
                fout.write(raw)
                continue
            pkg_norm = name_match.group(1).lower().replace("_", "-")
            if pkg_norm in drop_norm:
                dropped.append(stripped)
                fout.write(f"# (dropped by scripts/build_runtime/): {raw}")
            else:
                fout.write(raw)
    return dropped


def _site_packages_dir(python_dir: Path) -> Path:
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
            logger.info("stub skip: %s already exists in site-packages", name)
            continue
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init_py = pkg_dir / "__init__.py"
        if spec["type"] == "alias":
            alias_of = spec["alias_of"]
            init_py.write_text(
                "# stub by scripts/build_runtime/: " + name + " → " + alias_of + "\n"
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
                "# stub by scripts/build_runtime/: " + name + " (raises on use)",
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
