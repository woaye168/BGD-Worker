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
#   - **runtime 仅支持 GPT-SoVITS V4 模型**：base_models 走 V4 路径
#     （`s1v3.ckpt` + `gsv-v4-pretrained/s2Gv4.pth` + `chinese-roberta-wwm-ext-large/` + `chinese-hubert-base/`）。
#     meta.json:gpt_sovits_version 非 v4 时合成报错引导用户换 V4 模型。
#     V2/V3 模型支持留 future（需额外打 base models 包 + Pipeline 多实例缓存）。
#   - 每个 voice 模型目录 (data_dir/models/<id>/) 必含 meta.json，至少声明：
#       gpt_weights, sovits_weights, ref_audio, ref_text, ref_lang(默认 zh), text_lang(默认 zh)
#     缺字段则按文件名约定回退（gpt.ckpt / sovits.pth / ref.wav）；
#     gpt_sovits_version 默认 v4（与 runtime 版本对齐）

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
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
        "version": meta.get("gpt_sovits_version", "v4"),
    }


def _ensure_pipeline() -> object:
    """懒构造 GPT-SoVITS TTS pipeline；返回 pipeline 实例。

    要求：runtime_root 下存在 GPT_SoVITS/ 与 base_models/。
    每一步都 log，方便排查"哪一步炸了"——首次合成超时长，没日志根本不知道在干嘛。
    """
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE

    t0 = time.time()
    gpt_sovits_root = _RUNTIME_ROOT  # GPT_SoVITS/ 直接在 runtime_root 下
    base_models = _RUNTIME_ROOT / "base_models"
    logger.info(
        "pipeline init starting: runtime_root=%s gpt_sovits_dir=%s base_models=%s",
        _RUNTIME_ROOT, gpt_sovits_root / "GPT_SoVITS", base_models,
    )

    if not (gpt_sovits_root / "GPT_SoVITS").exists():
        raise FileNotFoundError(
            f"GPT_SoVITS code not found at {gpt_sovits_root / 'GPT_SoVITS'} "
            "（runtime 包损坏？请重装）"
        )
    if not base_models.exists():
        raise FileNotFoundError(
            f"base_models not found at {base_models}（runtime 包损坏？请重装）"
        )

    # 检查关键 base models 文件 —— 早炸早知道，比 GPT-SoVITS 抛模糊错误强。
    # V4 配置（与 GPT-SoVITS 20250422v4 tag 的 tts_infer.yaml `v4` 段对齐）：
    #   t2s = s1v3.ckpt（V3+V4 共用 GPT 权重）
    #   vits = gsv-v4-pretrained/s2Gv4.pth（V4 SoVITS 主权重，~769MB）
    #   bert = chinese-roberta-wwm-ext-large/（V2/V3/V4 共用）
    #   cnhubert = chinese-hubert-base/（V2/V3/V4 共用）
    required = [
        base_models / "s1v3.ckpt",
        base_models / "gsv-v4-pretrained" / "s2Gv4.pth",
        base_models / "chinese-roberta-wwm-ext-large",
        base_models / "chinese-hubert-base",
    ]
    for p in required:
        if not p.exists():
            raise FileNotFoundError(
                f"base_models 缺关键文件/目录: {p}（runtime 包不完整？请重装）"
            )
        if p.is_file() and p.stat().st_size == 0:
            raise FileNotFoundError(f"base_models 文件为空: {p}（下载损坏？请重装）")

    # GPT-SoVITS 内部用相对路径（如 GPT_SoVITS/configs/...）；切到 runtime_root 让 import 与 path 解析正确
    os.chdir(str(_RUNTIME_ROOT))

    # G2PW (chinese2.py) 在模块级读取 os.environ["bert_path"] 作为 AutoTokenizer 的 model_source；
    # 默认是相对路径 "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"，
    # 但该目录实际在 base_models/ 下。提前注入绝对路径避免 transformers 把它当成 HF Hub repo ID。
    os.environ["bert_path"] = str(base_models / "chinese-roberta-wwm-ext-large")

    # 两层 sys.path 都加：
    # - runtime_root：让 `from GPT_SoVITS.TTS_infer_pack ...` 工作
    # - runtime_root/GPT_SoVITS：让 GPT-SoVITS 内部的顶级导入 `from AR.models...` /
    #   `from module.xxx import yyy` / `from text.symbols import ...` 工作
    #   （GPT-SoVITS 假设运行时 cwd 是 GPT_SoVITS/ 子目录，AR/module/text/feature_extractor
    #    等都是兄弟包，他们的 import 都没用相对 import，必须靠 sys.path）
    for p in (_RUNTIME_ROOT, _RUNTIME_ROOT / "GPT_SoVITS"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)

    # 报告关键依赖版本（torch / transformers / GPT-SoVITS）——出错时知道是不是 nightly 漂移
    logger.info("loading torch ...")
    try:
        import torch  # type: ignore[import-not-found]

        logger.info(
            "torch loaded: version=%s cuda_available=%s mps_available=%s",
            torch.__version__,
            torch.cuda.is_available(),
            getattr(torch.backends, "mps", None) and torch.backends.mps.is_available(),
        )
        if torch.cuda.is_available():
            logger.info(
                "cuda device: name=%s count=%d",
                torch.cuda.get_device_name(0),
                torch.cuda.device_count(),
            )
    except Exception as e:
        logger.exception("torch load failed")
        raise RuntimeError(f"torch 加载失败: {e}") from e

    # GPT-SoVITS tools/my_utils.py 的 load_audio 依赖 ffmpeg 命令行。
    # 嵌入式 Python 不含系统 ffmpeg；用 imageio-ffmpeg 提供的二进制并注入 PATH。
    logger.info("ensure ffmpeg in PATH (via imageio_ffmpeg)")
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
        import os as _os

        _ffmpeg_dir = _os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
        _path_sep = ";" if _os.name == "nt" else ":"
        _env_path = _os.environ.get("PATH", "")
        if _ffmpeg_dir not in _env_path.split(_path_sep):
            _os.environ["PATH"] = _ffmpeg_dir + _path_sep + _env_path
            logger.info("ffmpeg dir added to PATH: %s", _ffmpeg_dir)
    except Exception as e:
        logger.warning("imageio_ffmpeg setup failed: %s", e)

    # torchaudio 2.6+ 默认后端 torchcodec 依赖 FFmpeg DLL（嵌入式环境无）。
    # GPT-SoVITS TTS.py:_get_ref_spec 第 1 步调用 torchaudio.load 读参考音频，
    # 用 soundfile 替代避免 torchcodec 缺失/不兼容问题。
    logger.info("patch torchaudio.load -> soundfile")
    try:
        import torchaudio  # type: ignore[import-not-found]
        import soundfile as sf  # type: ignore[import-not-found]

        _orig_torchaudio_load = torchaudio.load

        def _torchaudio_load_with_soundfile(filepath, *args, **kwargs):
            data, sr = sf.read(str(filepath), dtype="float32")
            if data.ndim == 1:
                tensor = torch.from_numpy(data).unsqueeze(0)
            else:
                tensor = torch.from_numpy(data.T)
            return tensor, sr

        torchaudio.load = _torchaudio_load_with_soundfile  # type: ignore[assignment]
    except Exception as e:
        logger.warning("torchaudio.load patch failed: %s", e)

    # 懒导入 —— 缺依赖时给清晰错误
    logger.info("loading GPT_SoVITS.TTS_infer_pack ...")
    try:
        from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # type: ignore[import-not-found]
    except ImportError as e:
        logger.exception("GPT_SoVITS import failed")
        raise RuntimeError(
            f"无法 import GPT_SoVITS: {e}（确认 runtime 包中含 GPT_SoVITS/ 与其依赖；"
            f"可能某个被 stub/drop 的包在模块级被 GPT-SoVITS 引用）"
        ) from e

    # 设备 + 半精度策略：
    # - NVIDIA CUDA：fp16 加速（fp32 兜底）
    # - AMD ROCm (torch.version.hip is not None)：**强制 fp32**
    #   - RDNA3/3.5 (gfx1151/Strix Halo) 上 MIOpen FP16 路径优化未必比 FP32 快
    #   - 实测见日志：is_half=True 下 0.15× realtime 远不及预期；先 fp32 看稳定 baseline
    # - cpu / directml：必须 fp32
    # ROCm 检测：PyTorch ROCm 把 torch.cuda 接口接到 HIP，torch.version.cuda 也有值，
    # 但 torch.version.hip 仅在 ROCm 编译版本上非空 —— 这是区分 NV CUDA 与 AMD ROCm 的可靠信号
    _is_rocm = getattr(torch.version, "hip", None) is not None  # type: ignore[attr-defined]
    is_half = (_BACKEND == "cuda") and not _is_rocm
    device_str = "cuda" if _BACKEND == "cuda" else "cpu"
    logger.info(
        "device select: backend=%s torch.version.cuda=%s torch.version.hip=%s is_rocm=%s is_half=%s",
        _BACKEND,
        getattr(torch.version, "cuda", None),
        getattr(torch.version, "hip", None),
        _is_rocm, is_half,
    )
    # DirectML 当前仍走 cpu 设备字符串；torch_directml 的真正接入需要 GPT-SoVITS 代码改造，
    # 此处先用 cpu 兜底保证可用性（AMD 用户落 CPU 模式仍能合成，只是较慢）。

    # V4 config（与 GPT-SoVITS 20250422v4 tag 的 tts_infer.yaml v4 段一致）
    # TTS_Config 期望结构 {"version": "v4", "custom": {...}}，不是 {"default": {...}}
    config_dict = {
        "version": "v4",
        "custom": {
            "device": device_str,
            "is_half": is_half,
            "version": "v4",
            "t2s_weights_path": str(base_models / "s1v3.ckpt"),
            "vits_weights_path": str(base_models / "gsv-v4-pretrained" / "s2Gv4.pth"),
            "bert_base_path": str(base_models / "chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(base_models / "chinese-hubert-base"),
        }
    }
    logger.info(
        "init TTS_Config: backend=%s device=%s is_half=%s version=v4",
        _BACKEND, device_str, is_half,
    )
    try:
        config = TTS_Config(config_dict)
    except Exception as e:
        logger.exception("TTS_Config construct failed")
        raise RuntimeError(f"TTS_Config 构造失败: {e}") from e

    logger.info("init TTS pipeline (loads BERT/HuBERT/base GPT/base SoVITS) ...")
    try:
        _PIPELINE = TTS(config)
    except Exception as e:
        logger.exception("TTS pipeline construct failed")
        raise RuntimeError(f"TTS pipeline 构造失败: {e}") from e

    # 上游预训练权重（chinese-hubert-base / chinese-roberta-wwm-ext-large）
    # 可能是 FP16 保存的；CPU 模式下 is_half=False 需要显式转 FP32，
    # 否则 FP16 权重与 FP32 输入在 conv1d 中类型不匹配。
    if not is_half:
        if getattr(_PIPELINE, "cnhuhbert_model", None) is not None:
            _PIPELINE.cnhuhbert_model = _PIPELINE.cnhuhbert_model.float()
            logger.info("cnhuhbert_model -> float32")
        if getattr(_PIPELINE, "bert_model", None) is not None:
            _PIPELINE.bert_model = _PIPELINE.bert_model.float()
            logger.info("bert_model -> float32")

    logger.info("pipeline ready in %.1fs", time.time() - t0)
    return _PIPELINE


def _switch_voice(model_id: str) -> dict:
    """切到指定 model_id 的权重；返回该模型的 meta 信息（含路径）。"""
    global _CURRENT_VOICE
    meta = _read_meta(model_id)
    # V4-only：meta.json 声明非 v4 时拒绝，引导用户换 V4 模型
    if meta["version"] != "v4":
        raise RuntimeError(
            f"模型 {model_id} 声明 gpt_sovits_version={meta['version']!r}，"
            f"本 runtime 仅支持 v4 模型；请去模型分享站找 V4 版本，或在 meta.json 改 "
            f"gpt_sovits_version=\"v4\"（仅当确认该模型是 V4 训练时）"
        )
    pipeline = _ensure_pipeline()
    if _CURRENT_VOICE != model_id:
        # 校验文件存在
        for key in ("gpt_weights", "sovits_weights", "ref_audio"):
            p = meta[key]
            if not p.exists():
                raise FileNotFoundError(f"model file missing: {p}")
        t0 = time.time()
        logger.info(
            "switch voice: %s → %s (gpt=%s sovits=%s ref=%s ref_text_len=%d ref_lang=%s text_lang=%s)",
            _CURRENT_VOICE, model_id,
            meta["gpt_weights"].name, meta["sovits_weights"].name, meta["ref_audio"].name,
            len(meta["ref_text"]), meta["ref_lang"], meta["text_lang"],
        )
        try:
            pipeline.init_t2s_weights(str(meta["gpt_weights"]))  # type: ignore[attr-defined]
            pipeline.init_vits_weights(str(meta["sovits_weights"]))  # type: ignore[attr-defined]
        except Exception as e:
            logger.exception("init voice weights failed")
            raise RuntimeError(f"加载模型权重失败 (model={model_id}): {e}") from e
        _CURRENT_VOICE = model_id
        logger.info("voice loaded in %.1fs", time.time() - t0)
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
        t_infer = time.time()
        logger.info(
            "synthesize start: model=%s text_len=%d text_preview=%r emotion=%s rate=%.2f",
            model_id, len(text), text[:50], emotion, rate,
        )
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
        infer_secs = time.time() - t_infer
        logger.info(
            "synthesize done: samples=%d sr=%d duration=%.2fs infer_time=%.1fs (%.1fx realtime)",
            audio.shape[0], sr, audio.shape[0] / sr, infer_secs,
            (audio.shape[0] / sr) / infer_secs if infer_secs > 0 else 0,
        )

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


def _force_utf8_io() -> None:
    """让本进程的 stdout/stderr 与 Windows 控制台 codepage 一致走 UTF-8。

    背景：runtime 子进程的 stdout/stderr 被主 app 重定向到文件
    （data_dir/logs/local-tts-runtime.log）。该文件被多处并发写：
      1. Python logger / print → 受 `-X utf8` 影响走 UTF-8 字节
      2. C 扩展（onnxruntime / torch 等）→ 走 Windows 默认 ANSI codepage（GBK/cp936）
         或在出错时调 wide-char API 写 UTF-16 LE 原始字节
    三种编码混到同一个文件 → 后期 reader 用 UTF-8 解码就出乱码。

    修法：
      - sys.stdout/stderr.reconfigure(encoding='utf-8')：Python 侧统一 UTF-8
      - Windows: SetConsoleOutputCP(65001) + SetConsoleCP(65001)：
        C 扩展走 narrow-byte 路径时也用 UTF-8，wide-char API 经此 codepage 转 UTF-8

    无副作用：非 Windows 跳过 codepage 调用；reconfigure 失败 silently 兜底。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def _apply_perf_env_vars() -> None:
    """启动期注入 MIOpen / ORT 性能相关环境变量。

    必须在 `import torch` 之前生效（_ensure_pipeline 内才懒导入 torch；这里 setenv 时机够早）。

    MIOpen（AMD ROCm 上的 cuDNN 等价物）：
      - MIOPEN_FIND_MODE=FAST（2）：**只查 disk cache 不重新评估** —— Kokoro-FastAPI Issue #454
        在 Strix Halo gfx1151 上实测 12-14× 加速（13s → 1.1s）的关键开关。
        与 NORMAL（1）的区别：
          NORMAL = 每次新进程都 search + 评估 solver + 写 user db（per-process penalty）
          FAST   = 只查 disk cache；hit 直接用（ms 级），miss 走 WTI fallback（一次开销）
        cache 通过自然合成累积；用户跑过的 conv shape 越多 hit 率越高。
        首次合成短句仍可能 30s+（cache miss 走 fallback），但跑过同 voice + 类似长度后
        次启动直接 lookup ms 级。
      - MIOPEN_DEBUG_DISABLE_FIND_DB=0：保持 db 启用（cache hit 必需）

    ONNX Runtime（GPT-SoVITS G2PW 多音字消解用到）：
      - ORT_DISABLE_ALL_PROVIDERS_BUT_CPU=1：禁止加载 CUDA EP DLL，避免 cublasLt 错误日志

    用 setdefault：用户/CI 显式覆盖优先。
    """
    perf_env = {
        "MIOPEN_FIND_MODE": "FAST",
        "MIOPEN_DEBUG_DISABLE_FIND_DB": "0",
        "ORT_DISABLE_ALL_PROVIDERS_BUT_CPU": "1",
    }
    applied = []
    for k, v in perf_env.items():
        if k not in os.environ:
            os.environ[k] = v
            applied.append(f"{k}={v}")
    if applied:
        logger.info("perf env vars applied: %s", " ".join(applied))


def main(argv: Optional[list[str]] = None) -> int:
    global _BACKEND, _MOCK, _MODEL_ROOT
    _force_utf8_io()  # 第一时间统一 IO 编码，下面所有 print/log 都走 UTF-8
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

    # MIOpen / ORT 性能环境变量（必须在 torch / onnxruntime import 前；torch 在 _ensure_pipeline
    # 内才懒导入，main 后期 setenv 仍来得及）。放在 basicConfig 之后让 logger.info 能输出。
    _apply_perf_env_vars()

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
