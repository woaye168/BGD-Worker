"""V4 推理参数测速脚本 — 在 amd-rocm runtime 上跑，找出 sample_steps 的最佳 tradeoff。

用途：
  排查 GPT-SoVITS V4 在 AMD GPU 上 0.2× realtime 慢的真凶。
  根据 api_v2.py 默认参数对比，我们 serve.py 漏传 `sample_steps`（默认 32）—
  这是 V4 vocoder 的 CFM 去噪步数，每个时间步跑 32 次 forward。
  实测 8/16/32/4 各种值的耗时 + sample audio，得出最佳默认值。

如何跑：
  在已装 amd-rocm runtime 的 Windows 机器上：

  cd C:\\Users\\<you>\\.npc-voice-gen\\runtimes\\local-tts-amd-rocm
  python.exe scripts_benchmark.py --model-root C:\\Users\\<you>\\.npc-voice-gen\\models --voice <你常用 voice id>

  （把 scripts/benchmark_v4_speed.py 复制到 runtime 解压目录改名 scripts_benchmark.py
   再跑，因为它要用 runtime 内的 python.exe 装的 torch + GPT-SoVITS）

输出：
  - 每个 sample_steps 值跑 3 句话，记录平均耗时 + realtime ratio
  - 保存 sample_*.wav 到 cwd，用户主观听对比质量

注意：
  - 第一句会触发模型加载 + MIOpen kernel 编译，**不算入平均**
  - 切换 sample_steps 不需要重新加载模型，pipeline 实例复用
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import wave
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("benchmark")

# 设性能环境变量（同 serve.py）
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ.setdefault("MIOPEN_DEBUG_DISABLE_FIND_DB", "0")
os.environ.setdefault("ORT_DISABLE_ALL_PROVIDERS_BUT_CPU", "1")


def _setup_runtime_paths(runtime_root: Path) -> None:
    """对齐 serve.py 的 sys.path 与 cwd 设置。"""
    os.chdir(str(runtime_root))
    for p in (runtime_root, runtime_root / "GPT_SoVITS"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
    os.environ["bert_path"] = str(
        runtime_root / "base_models" / "chinese-roberta-wwm-ext-large"
    )


def _build_pipeline(runtime_root: Path):
    """构造 GPT-SoVITS V4 pipeline；与 serve.py 一致。"""
    import torch

    is_rocm = getattr(torch.version, "hip", None) is not None
    is_half = torch.cuda.is_available() and not is_rocm
    device_str = "cuda" if torch.cuda.is_available() else "cpu"

    logger.info(
        "torch=%s cuda_available=%s hip=%s is_rocm=%s is_half=%s device=%s",
        torch.__version__,
        torch.cuda.is_available(),
        getattr(torch.version, "hip", None),
        is_rocm,
        is_half,
        device_str,
    )

    base_models = runtime_root / "base_models"
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

    config = TTS_Config({
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
    })
    logger.info("init pipeline ...")
    t0 = time.time()
    pipeline = TTS(config)
    logger.info("pipeline ready in %.1fs", time.time() - t0)
    return pipeline


def _switch_voice(pipeline, model_dir: Path, meta: dict) -> dict:
    """加载 voice 权重并返回 ref 路径与 ref_text。"""
    gpt = model_dir / meta.get("gpt_weights", "gpt.ckpt")
    sovits = model_dir / meta.get("sovits_weights", "sovits.pth")
    ref_audio = model_dir / meta.get("ref_audio", "ref.wav")
    logger.info("switch voice: gpt=%s sovits=%s ref=%s", gpt.name, sovits.name, ref_audio.name)
    t0 = time.time()
    pipeline.init_t2s_weights(str(gpt))
    pipeline.init_vits_weights(str(sovits))
    logger.info("voice loaded in %.1fs", time.time() - t0)
    return {
        "ref_audio_path": str(ref_audio),
        "prompt_text": meta.get("ref_text", ""),
        "prompt_lang": meta.get("ref_lang", "zh"),
        "text_lang": meta.get("text_lang", "zh"),
    }


def _synthesize(pipeline, base_req: dict, text: str, sample_steps: int, super_sampling: bool):
    """单次合成；返回 (耗时秒, sr, np.ndarray音频, 持续秒)。"""
    import numpy as np
    req = dict(base_req)
    req.update({
        "text": text,
        "text_split_method": "cut5",
        "batch_size": 1,
        "speed_factor": 1.0,
        "streaming_mode": False,
        # 关键变量：V4 vocoder CFM 步数
        "sample_steps": sample_steps,
        "super_sampling": super_sampling,
        # 显式带上其他官方默认（与 api_v2 对齐）
        "top_k": 5,
        "top_p": 1.0,
        "temperature": 1.0,
        "repetition_penalty": 1.35,
        "parallel_infer": True,
        "batch_threshold": 0.75,
        "split_bucket": True,
        "fragment_interval": 0.3,
        "seed": -1,
    })
    chunks = []
    sr = 32000
    t0 = time.time()
    for chunk_sr, audio_chunk in pipeline.run(req):
        sr = int(chunk_sr)
        chunks.append(audio_chunk)
    secs = time.time() - t0
    audio = np.concatenate(chunks, axis=0) if chunks else np.zeros(sr, dtype="float32")
    return secs, sr, audio, audio.shape[0] / sr


def _save_wav(path: Path, audio, sr: int) -> None:
    import soundfile as sf
    sf.write(str(path), audio, sr, format="WAV", subtype="PCM_16")


def main() -> int:
    parser = argparse.ArgumentParser(description="GPT-SoVITS V4 sample_steps benchmark")
    parser.add_argument(
        "--runtime-root",
        type=str,
        default=str(Path(__file__).resolve().parent),
        help="runtime 解压目录（默认：脚本所在目录）",
    )
    parser.add_argument(
        "--model-root",
        type=str,
        required=True,
        help="data_dir/models 目录（含 voice 子目录）",
    )
    parser.add_argument("--voice", type=str, required=True, help="voice id（model_root 下的子目录名）")
    parser.add_argument(
        "--steps",
        type=str,
        default="32,16,8,4",
        help="逗号分隔的 sample_steps 取值序列",
    )
    parser.add_argument(
        "--text",
        type=str,
        default="哎呀，这不是我们须弥的草神大人吗？难得在稻妻遇见，该不会是特意来拜访我的吧？",
        help="测试文本（默认 33 字典型短句）",
    )
    parser.add_argument("--repeats", type=int, default=3, help="每个步数跑几次取平均（首次不计入）")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="./benchmark_out",
        help="sample wav 输出目录",
    )
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    model_root = Path(args.model_root).resolve()
    model_dir = model_root / args.voice
    if not model_dir.exists():
        raise SystemExit(f"voice 目录不存在：{model_dir}")
    meta_file = model_dir / "meta.json"
    if not meta_file.exists():
        raise SystemExit(f"meta.json 不存在：{meta_file}")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    _setup_runtime_paths(runtime_root)
    pipeline = _build_pipeline(runtime_root)
    base_req = _switch_voice(pipeline, model_dir, meta)

    # 预热：跑一次丢弃（触发 MIOpen kernel 编译 + cache 填充）
    logger.info("=== warmup (discard) ===")
    _synthesize(pipeline, base_req, args.text, sample_steps=32, super_sampling=False)
    logger.info("warmup done")

    steps_list = [int(s) for s in args.steps.split(",") if s.strip()]
    results = []
    for steps in steps_list:
        logger.info("=== sample_steps=%d (%d repeats) ===", steps, args.repeats)
        times = []
        last_audio = None
        last_sr = 32000
        last_dur = 0.0
        for i in range(args.repeats):
            secs, sr, audio, dur = _synthesize(
                pipeline, base_req, args.text, sample_steps=steps, super_sampling=False
            )
            ratio = dur / secs if secs > 0 else 0
            logger.info("  [steps=%d run=%d] infer=%.2fs audio=%.2fs realtime_ratio=%.2fx", steps, i + 1, secs, dur, ratio)
            times.append(secs)
            last_audio, last_sr, last_dur = audio, sr, dur
        avg = sum(times) / len(times)
        ratio_avg = last_dur / avg if avg > 0 else 0
        results.append({"sample_steps": steps, "avg_secs": avg, "duration_secs": last_dur, "realtime_ratio": ratio_avg})
        wav_path = out_dir / f"sample_steps_{steps}.wav"
        _save_wav(wav_path, last_audio, last_sr)
        logger.info("  → 平均 %.2fs (ratio %.2fx) → 保存 %s", avg, ratio_avg, wav_path)

    # 汇总
    print("\n========== 汇总 ==========")
    print(f"voice={args.voice} text='{args.text[:30]}...' ({len(args.text)} 字)")
    print(f"{'steps':>6} | {'avg_secs':>10} | {'audio_dur':>10} | {'realtime':>10}")
    print("-" * 50)
    for r in results:
        print(
            f"{r['sample_steps']:>6} | {r['avg_secs']:>9.2f}s | "
            f"{r['duration_secs']:>9.2f}s | {r['realtime_ratio']:>8.2f}×"
        )
    print(f"\n  WAV 文件在: {out_dir}")
    print("  对比同一句话用不同步数合成的音质 + 耗时，决定 serve.py 默认值\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
