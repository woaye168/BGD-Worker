# @purpose: 本地 TTS 运行时 HTTP 服务（嵌入到 runtime 包内，由主 app 子进程方式启动）
# @layer: runtime (独立进程，非 app 一部分)
# @contract:
#   - main()：argparse 入口，命令行 `python serve.py --port N --model-root P
#     [--backend auto|cuda|directml|cpu] [--mock]`
#     运行时根目录由脚本所在目录决定（runtime_root = Path(__file__).parent），
#     从其下找 `GPT_SoVITS/`（推理代码）与 `base_models/`（共享基础模型）。
#   - HTTP API:
#     - GET /health  -> {"status":"ready","backend":str,"mock":bool}
#     - POST /synthesize {"text","model_id","emotion"?,"rate"?,"pitch"?,"volume"?} -> audio/wav bytes
#   - synthesize_wav(text, model_id, ...) -> bytes  # 真实 GPT-SoVITS 推理 / --mock 静默
#   - detect_backend(requested) -> str  # auto → cuda/directml/cpu
# @depends:
#   - fastapi, uvicorn, pydantic, numpy, soundfile (运行时包内必装)
#   - torch (运行时包内必装；可选 torch_directml)
#   - GPT-SoVITS 源码树（runtime_root/GPT_SoVITS/）+ 基础模型（runtime_root/base_models/）
# @invariants:
#   - 此文件 NOT 在主 app 进程导入；它在 data_dir/runtimes/local-tts/ 解压目录下、用嵌入式 python 跑
#   - 不依赖主 app 的 contract/api 层；与主 app 通信通过 HTTP（端口由主 app 选定后传入 --port）
#   - --mock 模式：忽略 GPT-SoVITS，返回 1 秒静默 16kHz WAV；供 IPC 链路冒烟测试
#   - 真实推理：模块级懒导入 + 全局 Pipeline 单例 + 当前 voice id 缓存（切 model_id 时换权重）
#   - /health 在模型未加载也能返回 ready；模型加载在第一次 /synthesize 触发
#   - 端口必须由调用方指定（--port），不允许 0（自由端口需主 app 先 bind 抢占）
#   - 每个 voice 模型目录 (data_dir/models/<id>/) 必含 meta.json，至少声明：
#       gpt_weights, sovits_weights, ref_audio, ref_text, ref_lang(默认 zh), text_lang(默认 zh)
#     缺字段则按文件名约定回退（gpt.ckpt / sovits.pth / ref.wav）

from __future__ import annotations

import argparse
import io
import json
import logging
import os
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
_RUNTIME_ROOT: Path = Path(__file__).resolve().parent  # serve.py 所在目录

# Pipeline 单例 + 当前装载的 voice（切换时调 init_*_weights 换权重）
_PIPELINE: object = None
_CURRENT_VOICE: Optional[str] = None
_PIPELINE_LOCK = threading.Lock()


def detect_backend(requested: str) -> str:
    """auto → cuda > directml > cpu；显式值直接返回。"""
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


def _read_meta(model_id: str) -> dict:
    """读 data_dir/models/<id>/meta.json，给文件路径加默认回退。"""
    model_dir = _MODEL_ROOT / model_id
    meta_file = model_dir / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError(f"meta.json not found: {meta_file}")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    return {
        "model_dir": model_dir,
        "gpt_weights": model_dir / meta.get("gpt_weights", "gpt.ckpt"),
        "sovits_weights": model_dir / meta.get("sovits_weights", "sovits.pth"),
        "ref_audio": model_dir / meta.get("ref_audio", "ref.wav"),
        "ref_text": meta.get("ref_text", ""),
        "ref_lang": meta.get("ref_lang", "zh"),
        "text_lang": meta.get("text_lang", "zh"),
        "version": meta.get("gpt_sovits_version", "v2"),
    }


def _ensure_pipeline() -> object:
    """懒构造 GPT-SoVITS TTS pipeline；返回 pipeline 实例。

    要求：runtime_root 下存在 GPT_SoVITS/ 与 base_models/。
    """
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE

    gpt_sovits_root = _RUNTIME_ROOT  # GPT_SoVITS/ 直接在 runtime_root 下
    base_models = _RUNTIME_ROOT / "base_models"
    if not (gpt_sovits_root / "GPT_SoVITS").exists():
        raise FileNotFoundError(
            f"GPT_SoVITS code not found at {gpt_sovits_root / 'GPT_SoVITS'} "
            "（runtime 包损坏？请重装）"
        )
    if not base_models.exists():
        raise FileNotFoundError(
            f"base_models not found at {base_models}（runtime 包损坏？请重装）"
        )

    # GPT-SoVITS 内部用相对路径（如 GPT_SoVITS/configs/...）；切到 runtime_root 让 import 与 path 解析正确
    os.chdir(str(_RUNTIME_ROOT))
    if str(_RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(_RUNTIME_ROOT))

    # 懒导入 —— 缺依赖时给清晰错误
    try:
        from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            f"无法 import GPT_SoVITS: {e}（确认 runtime 包中含 GPT_SoVITS/ 与其依赖）"
        ) from e

    # 设备 + 半精度策略
    # - cuda：fp16 加速（fp32 兜底）
    # - cpu / directml：必须 fp32（half 不稳定）
    is_half = _BACKEND == "cuda"
    device_str = "cuda" if _BACKEND == "cuda" else "cpu"
    # DirectML 当前仍走 cpu 设备字符串；torch_directml 的真正接入需要 GPT-SoVITS 代码改造，
    # 此处先用 cpu 兜底保证可用性（AMD 用户落 CPU 模式仍能合成，只是较慢）。

    config_dict = {
        "default": {
            "device": device_str,
            "is_half": is_half,
            "version": "v2",
            "t2s_weights_path": str(
                base_models / "s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt"
            ),
            "vits_weights_path": str(base_models / "s2G488k.pth"),
            "bert_base_path": str(base_models / "chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(base_models / "chinese-hubert-base"),
        }
    }
    logger.info("init GPT-SoVITS pipeline: backend=%s device=%s is_half=%s", _BACKEND, device_str, is_half)
    config = TTS_Config(config_dict)
    _PIPELINE = TTS(config)
    return _PIPELINE


def _switch_voice(model_id: str) -> dict:
    """切到指定 model_id 的权重；返回该模型的 meta 信息（含路径）。"""
    global _CURRENT_VOICE
    meta = _read_meta(model_id)
    pipeline = _ensure_pipeline()
    if _CURRENT_VOICE != model_id:
        # 校验文件存在
        for key in ("gpt_weights", "sovits_weights", "ref_audio"):
            p = meta[key]
            if not p.exists():
                raise FileNotFoundError(f"model file missing: {p}")
        logger.info("switch voice: %s → %s", _CURRENT_VOICE, model_id)
        pipeline.init_t2s_weights(str(meta["gpt_weights"]))  # type: ignore[attr-defined]
        pipeline.init_vits_weights(str(meta["sovits_weights"]))  # type: ignore[attr-defined]
        _CURRENT_VOICE = model_id
    return meta


def synthesize_wav(
    text: str,
    model_id: str,
    emotion: str = "neutral",
    rate: float = 1.0,
    pitch: float = 1.0,
    volume: float = 1.0,
) -> bytes:
    if _MOCK:
        logger.info("mock synthesize: text=%r model=%s", text[:30], model_id)
        return _silence_wav(1.0)

    with _PIPELINE_LOCK:
        meta = _switch_voice(model_id)
        pipeline = _PIPELINE
        assert pipeline is not None

        # 懒导入 numpy/soundfile（避免 --mock 模式下强依赖）
        import numpy as np  # type: ignore[import-not-found]
        import soundfile as sf  # type: ignore[import-not-found]

        if not meta["ref_text"]:
            logger.warning(
                "model %s 的 meta.json 未提供 ref_text，GPT-SoVITS 零样本合成质量会下降",
                model_id,
            )

        req = {
            "text": text,
            "text_lang": meta["text_lang"],
            "ref_audio_path": str(meta["ref_audio"]),
            "prompt_text": meta["ref_text"],
            "prompt_lang": meta["ref_lang"],
            "text_split_method": "cut5",
            "batch_size": 1,
            "streaming_mode": False,
            "speed_factor": float(rate) if rate > 0 else 1.0,
            # GPT-SoVITS 不直接吃 pitch/volume；忽略 + 留给主 app 用 _ffmpeg 后处理（未来）
        }
        chunks = []
        sr = 32000
        try:
            for chunk_sr, audio_chunk in pipeline.run(req):  # type: ignore[attr-defined]
                sr = int(chunk_sr)
                chunks.append(audio_chunk)
        except Exception as e:
            logger.exception("GPT-SoVITS inference failed")
            raise RuntimeError(f"GPT-SoVITS 推理失败: {e}") from e

        if not chunks:
            raise RuntimeError("GPT-SoVITS 推理返回空音频")
        audio = np.concatenate(chunks, axis=0)

        # soundfile 写 WAV PCM16
        buf = io.BytesIO()
        sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()


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
        "serve start: host=%s port=%d backend=%s mock=%s model_root=%s runtime=%s",
        args.host, args.port, _BACKEND, _MOCK, _MODEL_ROOT, _RUNTIME_ROOT,
    )

    import uvicorn

    app = _build_app()
    # 关键：用 stdout 打印 "READY" 行让主 app 知道可以发请求（轮询 /health 是另一条等待路径）
    print(f"READY port={args.port} backend={_BACKEND} mock={_MOCK}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
