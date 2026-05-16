# @purpose: 本地 TTS 运行时 HTTP 服务（嵌入到 runtime 包内，由主 app 子进程方式启动）
# @layer: runtime (独立进程，非 app 一部分)
# @contract:
#   - main()：argparse 入口，命令行 `python serve.py --port N --model-root P [--backend auto|cuda|directml|cpu] [--mock]`
#   - HTTP API:
#     - GET /health  -> {"status":"ready","backend":str,"mock":bool}
#     - POST /synthesize {"text","model_id","emotion"?,"rate"?,"pitch"?,"volume"?} -> audio/wav bytes
#   - synthesize_wav(text, model_dir, ...) -> bytes  # 核心合成函数（GPT-SoVITS 调用点）
#   - detect_backend(requested) -> str  # auto → cuda/directml/cpu
# @depends:
#   - fastapi, uvicorn (运行时包内必装)
#   - torch (运行时包内必装；可选 torch_directml)
#   - GPT_SoVITS (运行时包内；缺失时仅 --mock 模式可用)
# @invariants:
#   - 此文件 NOT 在主 app 进程导入；它在 data_dir/runtimes/local-tts/ 解压目录下、用嵌入式 python 跑
#   - 不依赖主 app 的 contract/api 层；与主 app 通信通过 HTTP（端口由主 app 选定后传入 --port）
#   - --mock 模式：忽略 GPT-SoVITS，返回 1 秒静默 16kHz WAV；供 IPC 链路冒烟测试
#   - 真实推理：lazy 导入 GPT_SoVITS；模型按 model_id 缓存（避免重复加载）
#   - /health 在模型未加载也能返回 ready；模型加载在第一次 /synthesize 触发
#   - 端口必须由调用方指定（--port），不允许 0（自由端口需主 app 先 bind 抢占）

from __future__ import annotations

import argparse
import io
import logging
import sys
import threading
import wave
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("local_tts.serve")


class SynthesizeReq(BaseModel):
    """模块级定义；FastAPI 的 Pydantic v2 ForwardRef 解析需要在模块作用域可见。"""

    text: str
    model_id: str
    emotion: str = "neutral"
    rate: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0

# 全局后端 + 模式（main 启动期写入；handler 只读）
_BACKEND: str = "cpu"
_MOCK: bool = False
_MODEL_ROOT: Path = Path(".")

# model_id → 加载后的 inference handle（实际类型取决于 GPT-SoVITS API）
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = threading.Lock()


def detect_backend(requested: str) -> str:
    """auto → cuda > directml > cpu；显式值直接返回（不存在的会在加载时抛错）。"""
    if requested != "auto":
        return requested
    try:
        import torch  # type: ignore[import-not-found]

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    try:
        import torch_directml  # type: ignore[import-not-found]  # noqa: F401

        return "directml"
    except Exception:
        pass
    return "cpu"


def _silence_wav(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """生成静默 PCM16 mono WAV，用于 --mock 模式。"""
    n_samples = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


def _load_model(model_id: str) -> object:
    """按 model_id 加载模型（懒加载 + 缓存）。返回 GPT-SoVITS 推理 handle。"""
    with _MODEL_LOCK:
        if model_id in _MODEL_CACHE:
            return _MODEL_CACHE[model_id]
        model_dir = _MODEL_ROOT / model_id
        if not model_dir.exists():
            raise FileNotFoundError(f"model dir not found: {model_dir}")
        # 真实接入点：调 GPT_SoVITS 的 inference 加载逻辑
        # 当前阶段：未接入；--mock 模式下不会走到这里
        raise RuntimeError(
            "GPT-SoVITS 推理尚未在 serve.py 中接入；请使用 --mock 模式或等待后续版本"
        )


def synthesize_wav(
    text: str,
    model_id: str,
    emotion: str = "neutral",
    rate: float = 1.0,
    pitch: float = 1.0,
    volume: float = 1.0,
) -> bytes:
    if _MOCK:
        # --mock：返回 1 秒静默，证明 IPC 链路通
        logger.info("mock synthesize: text=%r model=%s len=%d", text[:30], model_id, len(text))
        return _silence_wav(1.0)
    _load_model(model_id)  # 当前 _load_model 即抛"未接入"；保留调用点占位
    raise RuntimeError("GPT-SoVITS 推理尚未在 serve.py 中接入（synthesize_wav 调用点）")


def _build_app():
    """构造 FastAPI app；延迟 import fastapi 以便 import serve 在缺 fastapi 时不炸（pydantic 是必装）。"""
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import Response

    app = FastAPI(title="local-tts-runtime", version="1.0.0")

    @app.get("/health")
    def health():
        return {"status": "ready", "backend": _BACKEND, "mock": _MOCK}

    @app.post("/synthesize")
    def synthesize(req: SynthesizeReq = Body(...)):  # FastAPI ≥0.136 需显式 Body
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="text is empty")
        if not req.model_id:
            raise HTTPException(status_code=400, detail="model_id is empty")
        try:
            wav = synthesize_wav(
                text=req.text,
                model_id=req.model_id,
                emotion=req.emotion,
                rate=req.rate,
                pitch=req.pitch,
                volume=req.volume,
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("synthesize failed")
            raise HTTPException(status_code=500, detail=str(e)) from e
        return Response(content=wav, media_type="audio/wav")

    return app


def main(argv: Optional[list[str]] = None) -> int:
    global _BACKEND, _MOCK, _MODEL_ROOT
    parser = argparse.ArgumentParser(description="local TTS runtime HTTP server")
    parser.add_argument("--port", type=int, required=True, help="HTTP 端口（主 app 选定后传入）")
    parser.add_argument(
        "--model-root", type=str, required=True, help="模型根目录（data_dir/models）"
    )
    parser.add_argument(
        "--backend",
        type=str,
        default="auto",
        choices=["auto", "cuda", "directml", "cpu"],
    )
    parser.add_argument("--mock", action="store_true", help="使用静默 WAV 跳过真实推理")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    _MOCK = args.mock
    _MODEL_ROOT = Path(args.model_root)
    _BACKEND = detect_backend(args.backend)
    logger.info(
        "serve start: host=%s port=%d backend=%s mock=%s model_root=%s",
        args.host, args.port, _BACKEND, _MOCK, _MODEL_ROOT,
    )

    import uvicorn

    app = _build_app()
    # 关键：用 stdout 打印 "READY" 行让主 app 知道可以发请求（轮询 /health 是另一条等待路径）
    print(f"READY port={args.port} backend={_BACKEND} mock={_MOCK}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
