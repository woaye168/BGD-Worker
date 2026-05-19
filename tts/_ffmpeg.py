# @purpose: ffmpeg 三级探测 + 音频转码工具（导出阶段唯一调用点）
# @layer: adapter
# @contract:
#   - resolve_ffmpeg(explicit) -> Optional[str]
#   - SUPPORTED_TRANSCODE: dict[str, tuple[str, str, list[str]]]  # target_format → (codec, container, extra_args)
#   - EXPORT_EXTENSIONS: dict[str, str]  # target_format → file extension
#   - EXPORT_MEDIA_TYPES: dict[str, str] # target_format → HTTP Content-Type
#   - transcode(audio_bytes, target_format, ffmpeg_path) -> bytes  # async
# @depends:
#   - asyncio, shutil (stdlib)
#   - imageio_ffmpeg (可选第三方，提供捆绑静态 ffmpeg)
#   - ../contract/errors.py: TTSError
# @invariants:
#   - resolve_ffmpeg 三级探测：显式路径 → 系统 PATH → imageio-ffmpeg 捆绑；全失败返回 None
#   - SUPPORTED_TRANSCODE 覆盖 wav/mp3/ogg(vorbis)/opus/flac/m4a/aac；
#     注意 ogg 用 libvorbis（最兼容），opus 单独 target 用 libopus，**不要把 opus 塞进 .ogg 容器**
#     —— 历史 bug：旧版用 (libopus, ogg) 产 Opus-in-Ogg，部分播放器认不出
#   - transcode 输入字节流由 ffmpeg 自动嗅探容器（支持 mp3/wav/ogg/raw/flac/...），输出由 target_format 决定
#   - ffmpeg 进程返回非 0 退出码 → 抛 TTSError，带 stderr 文本
#   - 缺 ffmpeg 路径时调用 transcode 抛 TTSError（不静默 fallback；调用方需先 resolve_ffmpeg 判空）
#   - **本模块仅服务导出阶段**：合成路径不再 transcode（引擎直接产原始格式落盘）

import asyncio
import shutil
from typing import Optional

from contract.errors import TTSError

# target_format → (ffmpeg codec, ffmpeg muxer/container, extra encode args)
# 选编码器优先：libvorbis（ogg 兼容性最好）/ libmp3lame（mp3 标杆）/ libopus（opus 专用）
# m4a 用 ipod muxer（ffmpeg 约定 m4a 容器名）；aac 用 adts 裸流容器
SUPPORTED_TRANSCODE: dict[str, tuple[str, str, list[str]]] = {
    "wav":  ("pcm_s16le",  "wav",  []),
    "mp3":  ("libmp3lame", "mp3",  ["-b:a", "192k"]),
    "ogg":  ("libvorbis",  "ogg",  ["-q:a", "5"]),       # Vorbis 质量 0-10，5 ≈ 160 kbps
    "opus": ("libopus",    "opus", ["-b:a", "96k"]),
    "flac": ("flac",       "flac", []),
    "m4a":  ("aac",        "ipod", ["-b:a", "192k"]),    # ipod muxer 产 m4a 容器
    "aac":  ("aac",        "adts", ["-b:a", "192k"]),    # adts 裸 AAC 流
}

# 导出文件扩展名（多数与 target 同名，但 m4a/aac 等容器/扩展不一定一致）
EXPORT_EXTENSIONS: dict[str, str] = {
    "wav":  "wav",
    "mp3":  "mp3",
    "ogg":  "ogg",
    "opus": "opus",
    "flac": "flac",
    "m4a":  "m4a",
    "aac":  "aac",
}

# HTTP Content-Type 映射（试听/下载用）
EXPORT_MEDIA_TYPES: dict[str, str] = {
    "wav":  "audio/wav",
    "mp3":  "audio/mpeg",
    "ogg":  "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "m4a":  "audio/mp4",
    "aac":  "audio/aac",
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
        raise TTSError(
            f"不支持转码目标格式: {target_format}（支持：{sorted(SUPPORTED_TRANSCODE.keys())}）"
        )
    if not ffmpeg_path:
        raise TTSError("ffmpeg 路径未配置，无法转码")
    codec, container, extra = SUPPORTED_TRANSCODE[target_format]
    args = [
        ffmpeg_path,
        "-i", "pipe:0",
        "-c:a", codec,
        *extra,
        "-f", container,
        "pipe:1",
        "-loglevel", "error",
    ]
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
        raise TTSError(
            f"ffmpeg 转码失败 (target={target_format}, codec={codec}, container={container})："
            f"{err.decode(errors='ignore')[:500]}"
        )
    return out


__all__ = [
    "resolve_ffmpeg",
    "transcode",
    "SUPPORTED_TRANSCODE",
    "EXPORT_EXTENSIONS",
    "EXPORT_MEDIA_TYPES",
]
