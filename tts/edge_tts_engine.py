# @purpose: edge-tts 引擎适配（合成 MP3 → 按需 ffmpeg 转码 OGG/WAV）
# @layer: adapter
# @contract:
#   - resolve_ffmpeg(explicit) -> Optional[str]    # 转发自 ./_ffmpeg.py，保留出口
#   - EdgeTTSEngine(output_format, ffmpeg_path).{output_extension, synthesize, list_voices}
# @depends:
#   - logging (stdlib)
#   - edge_tts (第三方，可选导入)
#   - ../contract/models.py: Emotion
#   - ../contract/errors.py: TTSError
#   - ../synthesis/emotion_mapper.py: get_prosody
#   - ./_ffmpeg.py: resolve_ffmpeg, transcode  (ffmpeg 探测+转码共享工具)
# @invariants:
#   - ffmpeg 探测/转码逻辑下沉到 ./_ffmpeg.py，本模块仅消费
#   - 要求 ogg/wav 但无 ffmpeg 时，构造期自动降级为 mp3（开箱即用优先，绝不因缺 ffmpeg 而完全不可用）
#   - mp3 格式走 edge-tts 原生输出零转码；ogg/wav 经 _ffmpeg.transcode 转码
#   - output_extension 反映"实际生效"格式（可能因降级而异于请求值）
#   - edge_tts 缺失时构造仍成功；synthesize 调用时再抛 TTSError
#   - 情感作用：emotion → get_prosody → 与角色 rate/pitch/volume 相乘 → 编码为 edge-tts 字符串参数
#   - voice 入参假定已被 dispatch 层剥掉 "edge:" 前缀（本引擎只认裸 voice id）

import logging
from typing import Optional

from contract.errors import TTSError
from contract.models import Emotion
from synthesis.emotion_mapper import get_prosody

from ._ffmpeg import resolve_ffmpeg, transcode  # noqa: F401  (resolve_ffmpeg 仍是本模块出口)

logger = logging.getLogger(__name__)

try:
    import edge_tts  # type: ignore[import-untyped]
except ImportError:
    edge_tts = None  # type: ignore[assignment]


class EdgeTTSEngine:
    def __init__(self, output_format: str = "ogg", ffmpeg_path: Optional[str] = None) -> None:
        requested = output_format
        fmt = output_format if output_format in ("ogg", "mp3", "wav") else "ogg"
        self._ffmpeg = resolve_ffmpeg(ffmpeg_path)
        if fmt != "mp3" and self._ffmpeg is None:
            fmt = "mp3"  # 缺 ffmpeg → 降级，保证可用
        self._format = fmt
        logger.info(
            "edge_tts engine: requested=%s effective=%s ffmpeg=%s",
            requested, self._format, self._ffmpeg or "<none>",
        )

    @property
    def output_extension(self) -> str:
        return self._format

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
        mp3 = await self._render_mp3(text, voice, emotion, rate, pitch, volume)
        if self._format == "mp3":
            return mp3
        return await transcode(mp3, self._format, self._ffmpeg)

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
