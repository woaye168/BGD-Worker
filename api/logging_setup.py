# @purpose: 全局日志初始化与配置（控制台 + 滚动文件 + 请求中间件）
# @layer: adapter
# @contract:
#   - setup_logging(settings: LogSettings, log_dir: Path) -> None
#   - install_request_logging(app: FastAPI) -> None
# @depends:
#   - logging, logging.handlers (stdlib)
#   - pathlib (stdlib)
#   - fastapi.Request, starlette.middleware.base.BaseHTTPMiddleware
#   - ../contract/config.py: LogSettings
# @invariants:
#   - setup_logging 幂等：重复调用会先清空 root logger 已装的 handler
#   - log.enabled=False 时 root level 设为 CRITICAL+1（实际等于完全静默，业务模块的 logger.* 调用不输出）
#   - to_file=True 时启用 RotatingFileHandler，文件名 app.log，单文件 1MB，保留 5 份
#   - 请求中间件记录 method/path/status/耗时；4xx/5xx 用 warning/error 级别
#   - 业务模块约定：logger = logging.getLogger(__name__)，由本模块 setup_logging 统一接管

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from contract.config import LogSettings


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(settings: LogSettings, log_dir: Path) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    if not settings.enabled:
        root.setLevel(logging.CRITICAL + 1)
        return

    level = _LEVELS.get(settings.level.lower(), logging.INFO)
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.to_file:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class _RequestLogger(BaseHTTPMiddleware):
    _logger = logging.getLogger("npc.request")

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            status = response.status_code if response else 500
            line = f"{request.method} {request.url.path} -> {status} ({elapsed_ms:.0f}ms)"
            if status >= 500:
                self._logger.error(line)
            elif status >= 400:
                self._logger.warning(line)
            else:
                self._logger.info(line)


def install_request_logging(app: FastAPI) -> None:
    app.add_middleware(_RequestLogger)
