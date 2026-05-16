# @purpose: ffmpeg 三级探测 + 音频转码工具（edge_tts_engine / local_engine 共享）
# @layer: adapter
# @contract:
#   - resolve_ffmpeg(explicit) -> Optional[str]
#   - SUPPORTED_TRANSCODE: dict[str, tuple[str, str]]  (target_format → (codec, container))
#   - transcode(audio_bytes, target_format, ffmpeg_path) -> bytes  # async
# @depends:
#   - asyncio, shutil (stdlib)
#   - imageio_ffmpeg (可选第三方，提供捆绑静态 ffmpeg)
#   - ../contract/errors.py: TTSError
# @invariants:
#   - resolve_ffmpeg 三级探测：显式路径 → 系统 PATH → imageio-ffmpeg 捆绑；全失败返回 None
#   - SUPPORTED_TRANSCODE 覆盖 ogg/wav；mp3 通常 passthrough（edge-tts 原生输出 mp3），不在此表
#   - transcode 输入字节流由 ffmpeg 自动嗅探容器（支持 mp3/wav/ogg/raw...），输出由 target_format 决定
#   - ffmpeg 进程返回非 0 退出码 → 抛 TTSError，带 stderr 文本
#   - 缺 ffmpeg 路径时调用 transcode 抛 TTSError（不静默 fallback；调用方需先 resolve_ffmpeg 判空）

import asyncio
import shutil
from typing import Optional

from contract.errors import TTSError

SUPPORTED_TRANSCODE: dict[str, tuple[str, str]] = {
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


async def transcode(audio_bytes: bytes, target_format: str, ffmpeg_path: Optional[str]) -> bytes:
    if target_format not in SUPPORTED_TRANSCODE:
        raise TTSError(f"不支持转码目标格式: {target_format}")
    if not ffmpeg_path:
        raise TTSError("ffmpeg 路径未配置，无法转码")
    codec, container = SUPPORTED_TRANSCODE[target_format]
    args = [ffmpeg_path, "-i", "pipe:0", "-c:a", codec]
    if target_format == "ogg":
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
        raise TTSError(f"ffmpeg 不可用：{ffmpeg_path}") from e
    out, err = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise TTSError(f"ffmpeg 转码失败：{err.decode(errors='ignore')}")
    return out


__all__ = ["resolve_ffmpeg", "transcode", "SUPPORTED_TRANSCODE"]
