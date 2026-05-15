# @purpose: FastAPI 应用工厂（装配路由、CORS、领域错误处理器、静态前端、日志）
# @layer: adapter
# @contract:
#   - create_app() -> FastAPI
# @depends:
#   - logging, sys, pathlib (stdlib)
#   - fastapi, fastapi.middleware.cors, fastapi.staticfiles
#   - ../contract/config.py: AppConfig
#   - ../contract/errors.py
#   - ./logging_setup.py
#   - ./routes_character.py, ./routes_dialogue.py, ./routes_synthesis.py
# @invariants:
#   - 路由前缀均以 /api 开头，便于静态资源挂载 / 不冲突
#   - 静态目录 web/ 若存在则挂载到 /，提供前端 SPA
#   - 冻结模式(PyInstaller)下 web/ 从 sys._MEIPASS 解析，开发模式从项目根解析
#   - 仅在此层注册全局异常处理器，业务模块不直接返回 HTTP 响应
#   - create_app 启动时按用户设置初始化 root logger 与请求中间件（幂等）

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from contract.config import AppConfig
from contract.errors import (
    DomainError,
    NotFoundError,
    StorageError,
    TTSError,
    ValidationError,
)

from .logging_setup import install_request_logging, setup_logging
from .routes_character import router as character_router
from .routes_dialogue import router as dialogue_router
from .routes_synthesis import router as synthesis_router

_logger = logging.getLogger("npc.app")


def create_app() -> FastAPI:
    cfg = AppConfig.load()
    cfg.ensure_dirs()
    setup_logging(cfg.log, cfg.log_dir)
    _logger.info("app starting: data_dir=%s log_level=%s audio_dir=%s",
                 cfg.data_dir, cfg.log.level, cfg.audio_dir)

    app = FastAPI(title="游戏NPC语音生成器", version="0.1.0")
    install_request_logging(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation(_: Request, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TTSError)
    async def _tts(_: Request, exc: TTSError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(StorageError)
    async def _storage(_: Request, exc: StorageError):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    app.include_router(character_router)
    app.include_router(dialogue_router)
    app.include_router(synthesis_router)

    web_dir = _web_dir()
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

    return app


def _web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "web"
    return Path(__file__).resolve().parent.parent / "web"
