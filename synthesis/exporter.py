# @purpose: 已合成音频批量打包为 ZIP（按场景/角色分目录 + 清单）+ 按 export_format 统一转码
# @layer: logic
# @contract:
#   - async build_zip(dialogues, characters_by_id, audio_store, *, export_format="wav",
#                     ffmpeg_path=None) -> bytes
# @depends:
#   - asyncio, io, json, logging, zipfile (stdlib)
#   - ../contract/models.py: Character, Dialogue
#   - ../contract/ports.py: AudioStore
#   - ../tts/_ffmpeg.py: transcode, EXPORT_EXTENSIONS, SUPPORTED_TRANSCODE, resolve_ffmpeg
# @invariants:
#   - 仅打包 audio_path 非空且文件实际存在的对话，跳过未合成项
#   - ZIP 内路径结构：[场景/]角色名/文件名.<export_format 对应扩展名>；场景为空时省略
#   - **导出阶段才转码**：合成路径已解耦不再 transcode，导出统一走 ffmpeg
#     - 若 export_format == 已落盘音频的源格式（推断自扩展名）→ passthrough 不转码
#     - 否则调 _ffmpeg.transcode 把 bytes 转到目标格式
#     - export_format 不在 SUPPORTED_TRANSCODE 中 → 抛 ValueError（路由层映射 502/400）
#   - 单条对话转码失败 → 该条跳过 + 写 manifest.errors（不阻塞其他对话）
#   - 同名冲突自动追加 _N 后缀，保证 ZIP 内无重名
#   - 附带 manifest.json：file→character/scene/emotion/text 的清单 + errors[] + meta

import asyncio
import io
import json
import logging
import zipfile

from contract.models import Character, Dialogue
from contract.ports import AudioStore
from tts._ffmpeg import (
    EXPORT_EXTENSIONS,
    SUPPORTED_TRANSCODE,
    resolve_ffmpeg,
    transcode,
)

logger = logging.getLogger(__name__)


def _safe(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in (name or ""))
    return cleaned.strip() or "未命名"


def _source_ext(audio_path: str) -> str:
    return audio_path.rsplit(".", 1)[-1].lower() if "." in audio_path else ""


async def build_zip(
    dialogues: list[Dialogue],
    characters_by_id: dict[str, Character],
    audio_store: AudioStore,
    *,
    export_format: str = "wav",
    ffmpeg_path: str | None = None,
) -> bytes:
    fmt = (export_format or "wav").lower()
    if fmt not in SUPPORTED_TRANSCODE:
        raise ValueError(
            f"不支持的导出格式: {fmt}（支持：{sorted(SUPPORTED_TRANSCODE.keys())}）"
        )
    target_ext = EXPORT_EXTENSIONS.get(fmt, fmt)
    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    # 如果用户全是 mp3 落盘且 export=mp3，根本不需要 ffmpeg；
    # 用 needs_ffmpeg 兜底校验：发现 mismatch 但 ffmpeg=None 时再抛
    needs_ffmpeg_check_pending = True

    buf = io.BytesIO()
    seen: set[str] = set()
    errors: list[dict] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest_files = []
        for d in dialogues:
            if not d.audio_path or not audio_store.exists(d.audio_path):
                continue
            character = characters_by_id.get(d.character_id)
            char_name = character.name if character else "未知角色"
            base = _safe(d.filename or d.id)
            folder = f"{_safe(d.scene)}/" if d.scene else ""
            src_ext = _source_ext(d.audio_path)

            try:
                src_bytes = audio_store.open(d.audio_path)
                # passthrough：源扩展与目标一致（无需转码）
                if src_ext == target_ext:
                    out_bytes = src_bytes
                else:
                    if needs_ffmpeg_check_pending:
                        if not ffmpeg:
                            raise RuntimeError(
                                "需要 ffmpeg 转码但未检测到 ffmpeg 可执行文件（请装 ffmpeg 或在设置中指定路径）"
                            )
                        needs_ffmpeg_check_pending = False
                    out_bytes = await transcode(src_bytes, fmt, ffmpeg)
            except Exception as e:
                logger.warning(
                    "export transcode failed: dialogue=%s src_ext=%s target=%s err=%s",
                    d.id, src_ext, fmt, e,
                )
                errors.append({
                    "dialogue_id": d.id,
                    "source_ext": src_ext,
                    "target_format": fmt,
                    "error": str(e)[:300],
                })
                continue

            arcname = f"{folder}{_safe(char_name)}/{base}.{target_ext}"
            n = 1
            while arcname in seen:
                arcname = f"{folder}{_safe(char_name)}/{base}_{n}.{target_ext}"
                n += 1
            seen.add(arcname)
            zf.writestr(arcname, out_bytes)
            manifest_files.append({
                "file": arcname,
                "character": char_name,
                "scene": d.scene,
                "emotion": d.emotion.value,
                "text": d.text,
                "source_ext": src_ext,
                "exported_ext": target_ext,
            })

        manifest = {
            "export_format": fmt,
            "target_ext": target_ext,
            "files": manifest_files,
            "errors": errors,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    logger.info(
        "build_zip done: dialogues=%d packed=%d errors=%d export_format=%s",
        len(dialogues), len(manifest_files), len(errors), fmt,
    )
    return buf.getvalue()
