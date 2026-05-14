# @purpose: 桌面应用入口（pywebview 原生窗口 + 后台 uvicorn 服务）
# @layer: adapter
# @contract:
#   - main() -> None
# @depends:
#   - socket, threading, time (stdlib)
#   - uvicorn, webview (第三方)
#   - ./api/app.py: create_app
#   - ./api/deps.py: get_config
# @invariants:
#   - uvicorn 在 daemon 后台线程运行，禁用 reload（reload 派生子进程，与 webview 主线程不兼容）
#   - 监听 127.0.0.1 上动态选取的空闲端口，避免与用户其他服务冲突
#   - 主线程阻塞在 webview.start()，窗口关闭即整个进程退出
#   - 启动前调用 get_config() 触发数据目录创建（冻结模式落用户家目录）

import socket
import threading
import time

import uvicorn
import webview

from api.app import create_app
from api.deps import get_config


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


def main() -> None:
    get_config()  # 触发数据目录创建
    host, port = "127.0.0.1", _free_port()
    app = create_app()

    def serve() -> None:
        uvicorn.run(app, host=host, port=port, log_level="warning")

    threading.Thread(target=serve, daemon=True).start()
    if not _wait_ready(host, port):
        raise RuntimeError("后台服务启动超时")

    webview.create_window(
        "游戏NPC语音生成器",
        f"http://{host}:{port}",
        width=1280,
        height=860,
        min_size=(960, 640),
    )
    webview.start()


if __name__ == "__main__":
    main()
