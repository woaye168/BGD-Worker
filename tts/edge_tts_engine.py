# @purpose: edge-tts 引擎适配（合成 MP3 → ffmpeg 转码 OGG/Opus）
# @layer: adapter
# @contract:
#   - EdgeTTSEngine(ffmpeg_path).{synthesize, list_voices}
# @depends:
#   - asyncio (stdlib)
#   - edge_tts (第三方，可选导入)
#   - ../contract/models.py: Emotion
#   - ../contract/errors.py: TTSError
#   - ../synthesis/emotion_mapper.py: get_prosody
# @invariants:
#   - edge_tts 缺失时构造仍成功；synthesize 调用时再抛 TTSError，便于服务启动期不强制依赖
#   - synthesize 返回完整 OGG/Opus 字节，由本地 ffmpeg 转码完成
#   - 情感作用方式：emotion → get_prosody → 与角色 rate/pitch/volume 相乘 → 编码为 edge-tts 的字符串参数
#   - rate / volume 用 "+N%"，pitch 用 "+NHz" 偏移（edge-tts 的 SSML 实参约定）

import asyncio
from typing import Optional

from contract.errors import TTSError
from contract.models import Emotion
from synthesis.emotion_mapper import get_prosody

try:
    import edge_tts  # type: ignore[import-untyped]
except ImportError:
    edge_tts = None  # type: ignore[assignment]


class EdgeTTSEngine:
    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg_path

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
        if edge_tts is None:
            raise TTSError("edge-tts 未安装；请运行 `uv add edge-tts`")
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

        mp3 = bytearray()
        try:
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    mp3.extend(chunk["data"])
        except Exception as e:
            raise TTSError(f"edge-tts 流式合成失败：{e}") from e

        if not mp3:
            raise TTSError("edge-tts 返回空音频")
        return await self._mp3_to_ogg(bytes(mp3))

    async def _mp3_to_ogg(self, mp3: bytes) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-i", "pipe:0",
                "-c:a", "libopus",
                "-b:a", "64k",
                "-f", "ogg",
                "pipe:1",
                "-loglevel", "error",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise TTSError(f"ffmpeg 未找到：{self._ffmpeg}。请安装 ffmpeg 或在 TTSSettings 配置路径") from e
        out, err = await proc.communicate(input=mp3)
        if proc.returncode != 0:
            raise TTSError(f"ffmpeg 转码失败：{err.decode(errors='ignore')}")
        return out
