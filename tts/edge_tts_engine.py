# @purpose: edge-tts 引擎适配（合成始终返回原始 MP3；不再 transcode）
# @layer: adapter
# @contract:
#   - EdgeTTSEngine().{output_extension, synthesize, list_voices}
# @depends:
#   - logging (stdlib)
#   - edge_tts (第三方，可选导入)
#   - ../contract/models.py: Emotion
#   - ../contract/errors.py: TTSError
#   - ../synthesis/emotion_mapper.py: get_prosody
# @invariants:
#   - edge-tts 原生输出 MP3；本引擎不转码，output_extension 固定 "mp3"
#   - 任何"换格式"需求由导出阶段（synthesis/exporter.py）走 ffmpeg 处理
#   - edge_tts 缺失时构造仍成功；synthesize 调用时再抛 TTSError
#   - 情感作用：emotion → get_prosody → 与角色 rate/pitch/volume 相乘 → 编码为 edge-tts 字符串参数
#   - voice 入参假定已被 dispatch 层剥掉 "edge:" 前缀（本引擎只认裸 voice id）

import logging

from contract.errors import TTSError
from contract.models import Emotion
from synthesis.emotion_mapper import get_prosody

# 仅作为 deps 注入的兼容出口（旧代码可能 from tts.edge_tts_engine import resolve_ffmpeg）
from ._ffmpeg import resolve_ffmpeg  # noqa: F401

logger = logging.getLogger(__name__)

try:
    import edge_tts  # type: ignore[import-untyped]
except ImportError:
    edge_tts = None  # type: ignore[assignment]


class EdgeTTSEngine:
    def __init__(self) -> None:
        logger.info("edge_tts engine: format=mp3 (always; transcode moved to exporter)")

    @property
    def output_extension(self) -> str:
        return "mp3"

    async def list_voices(self) -> list[dict]:
        if edge_tts is None:
            return []
        voices = await edge_tts.list_voices()
        return [
            {"id": v["ShortName"], "name": v.get("FriendlyName", v["ShortName"]),
             "lang": v.get("Locale", ""), "gender": v.get("Gender", "")}
            for v in voices
        ]

    async def synthesize(
        self,
        text: str,
        voice: str,
        emotion: Emotion,
        rate: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
    ) -> bytes:
        return await self._render_mp3(text, voice, emotion, rate, pitch, volume)

    async def _render_mp3(
        self, text: str, voice: str, emotion: Emotion,
        rate: float, pitch: float, volume: float,
    ) -> bytes:
        if edge_tts is None:
            raise TTSError("edge-tts 未安装；请运行 `uv sync`")
        if not text.strip():
            raise TTSError("文本为空")

        prosody = get_prosody(emotion)
        eff_rate = rate * prosody["rate_mul"]
        eff_volume = volume * prosody["volume_mul"]
        eff_pitch_offset = (pitch - 1.0) * 50 + prosody["pitch_offset_pct"]

        rate_str = f"{int(round((eff_rate - 1) * 100)):+d}%"
        volume_str = f"{int(round((eff_volume - 1) * 100)):+d}%"
        pitch_str = f"{int(round(eff_pitch_offset)):+d}Hz"

        try:
            comm = edge_tts.Communicate(
                text=text,
                voice=voice or "zh-CN-XiaoxiaoNeural",
                rate=rate_str,
                volume=volume_str,
                pitch=pitch_str,
            )
        except Exception as e:
            raise TTSError(f"edge-tts 初始化失败：{e}") from e

        buf = bytearray()
        try:
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk["data"])
        except Exception as e:
            raise TTSError(f"edge-tts 流式合成失败：{e}") from e

        if not buf:
            raise TTSError("edge-tts 返回空音频")
        return bytes(buf)
