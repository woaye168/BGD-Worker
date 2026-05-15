# @purpose: edge-tts 引擎适配（合成 MP3 → 按需 ffmpeg 转码 OGG/WAV）
# @layer: adapter
# @contract:
#   - resolve_ffmpeg(explicit) -> Optional[str]
#   - EdgeTTSEngine(output_format, ffmpeg_path).{output_extension, synthesize, list_voices}
# @depends:
#   - asyncio, shutil (stdlib)
#   - edge_tts (第三方，可选导入)
#   - imageio_ffmpeg (第三方，可选导入，提供捆绑的静态 ffmpeg)
#   - ../contract/models.py: Emotion
#   - ../contract/errors.py: TTSError
#   - ../synthesis/emotion_mapper.py: get_prosody
# @invariants:
#   - ffmpeg 三级探测：显式路径 → 系统 PATH → imageio-ffmpeg 捆绑；全失败则 _ffmpeg=None
#   - 要求 ogg/wav 但无 ffmpeg 时，构造期自动降级为 mp3（开箱即用优先，绝不因缺 ffmpeg 而完全不可用）
#   - mp3 格式走 edge-tts 原生输出零转码；ogg/wav 经 ffmpeg 转码
#   - output_extension 反映"实际生效"格式（可能因降级而异于请求值）
#   - edge_tts 缺失时构造仍成功；synthesize 调用时再抛 TTSError
#   - 情感作用：emotion → get_prosody → 与角色 rate/pitch/volume 相乘 → 编码为 edge-tts 字符串参数

import asyncio
import logging
import shutil
from typing import Optional

from contract.errors import TTSError
from contract.models import Emotion
from synthesis.emotion_mapper import get_prosody

logger = logging.getLogger(__name__)

try:
    import edge_tts  # type: ignore[import-untyped]
except ImportError:
    edge_tts = None  # type: ignore[assignment]

# 格式 → (ffmpeg 编码器, 容器)；mp3 不经 ffmpeg 故不在此表
_TRANSCODE = {
    "ogg": ("libopus", "ogg"),
    "wav": ("pcm_s16le", "wav"),
}


def resolve_ffmpeg(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


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
        return await self._transcode(mp3)

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

    async def _transcode(self, mp3: bytes) -> bytes:
        codec, container = _TRANSCODE[self._format]
        args = [self._ffmpeg, "-i", "pipe:0", "-c:a", codec]
        if self._format == "ogg":
            args += ["-b:a", "64k"]
        args += ["-f", container, "pipe:1", "-loglevel", "error"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise TTSError(f"ffmpeg 不可用：{self._ffmpeg}") from e
        out, err = await proc.communicate(input=mp3)
        if proc.returncode != 0:
            raise TTSError(f"ffmpeg 转码失败：{err.decode(errors='ignore')}")
        return out
