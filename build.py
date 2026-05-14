# @purpose: PyInstaller 打包脚本（把 desktop.py 打成单目录桌面应用）
# @layer: adapter
# @contract:
#   - build() -> None
# @depends:
#   - os, sys, pathlib (stdlib)
#   - PyInstaller (dev 依赖)
#   - imageio_ffmpeg (运行时依赖，用于定位静态 ffmpeg 二进制)
# @invariants:
#   - 入口固定为 desktop.py；产物名 NPC-Voice-Gen，置于 dist/
#   - web/ 前端目录与 imageio-ffmpeg 静态二进制必须随包，否则冻结后前端/转码不可用
#   - 打包前先调用 get_ffmpeg_exe() 物化 ffmpeg 二进制，确保 --collect-all 能收集到
#   - 不对 webview 用 --collect-all：它含 gtk/cocoa/qt 等跨平台后端子模块，
#     在当前 OS 上 import 会失败；改为依赖 PyInstaller 自带的平台感知 hook
#   - --add-data 分隔符随平台：Windows 用 ';'，其他用 ':'
#   - print 输出一律 ASCII：Windows 控制台默认 cp1252，中文会触发 UnicodeEncodeError
#   - 由 .github/workflows/ci.yml 在 windows/macos runner 上真实验证

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build() -> None:
    import PyInstaller
    import PyInstaller.__main__

    print(f"PyInstaller {PyInstaller.__version__} on {sys.platform}")

    try:
        import imageio_ffmpeg
        print("imageio-ffmpeg binary:", imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as e:  # noqa: BLE001 - 仅诊断，缺失由 --collect-all 兜底
        print(f"WARNING: cannot locate imageio-ffmpeg binary: {e}", file=sys.stderr)

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
        # webview 不用 --collect-all：其 platforms 子包含 gtk/cocoa/qt 等跨平台后端，
        # 在当前 OS 上 import 会失败；交给 PyInstaller 内置的平台感知 hook 处理。
        "--collect-submodules=uvicorn",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.loops.auto",
    ]
    print("PyInstaller args:", " ".join(args))
    PyInstaller.__main__.run(args)
    print("\nBuild complete -> dist/")


if __name__ == "__main__":
    try:
        build()
    except ImportError as e:
        print(f"Missing build dependency ({e}); run: uv sync", file=sys.stderr)
        sys.exit(1)
