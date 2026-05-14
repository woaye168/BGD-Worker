# @purpose: PyInstaller 打包脚本（把 desktop.py 打成单目录桌面应用）
# @layer: adapter
# @contract:
#   - build() -> None
# @depends:
#   - os, sys, pathlib (stdlib)
#   - PyInstaller (dev 依赖)
# @invariants:
#   - 入口固定为 desktop.py；产物名 NPC-Voice-Gen，置于 dist/
#   - web/ 前端目录与 imageio-ffmpeg 静态二进制必须随包，否则冻结后前端/转码不可用
#   - --add-data 分隔符随平台：Windows 用 ';'，其他用 ':'
#   - 沙箱无法验证：需在目标 OS（Win/Mac）本机执行 `uv run python build.py`

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build() -> None:
    import PyInstaller.__main__

    sep = ";" if os.name == "nt" else ":"
    args = [
        str(ROOT / "desktop.py"),
        "--name=NPC-Voice-Gen",
        "--windowed",
        "--noconfirm",
        "--clean",
        f"--add-data={ROOT / 'web'}{sep}web",
        "--collect-all=edge_tts",
        "--collect-all=imageio_ffmpeg",
        "--collect-all=webview",
        "--collect-submodules=uvicorn",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.loops.auto",
    ]
    print("PyInstaller args:", " ".join(args))
    PyInstaller.__main__.run(args)
    print("\n构建完成 → dist/NPC-Voice-Gen/")


if __name__ == "__main__":
    try:
        build()
    except ImportError:
        print("缺少 PyInstaller，请先执行：uv sync --all-extras", file=sys.stderr)
        sys.exit(1)
