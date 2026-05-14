# @purpose: 已合成音频批量打包为 ZIP（按场景/角色分目录 + 清单）
# @layer: logic
# @contract:
#   - build_zip(dialogues, characters_by_id, audio_store) -> bytes
# @depends:
#   - io, json, zipfile (stdlib)
#   - ../contract/models.py: Character, Dialogue
#   - ../contract/ports.py: AudioStore
# @invariants:
#   - 仅打包 audio_path 非空且文件实际存在的对话，跳过未合成项
#   - ZIP 内路径结构：[场景/]角色名/文件名.扩展名；场景为空时省略场景层
#   - 文件名扩展名取自 audio_path（落盘时已带正确扩展名），不重新推断
#   - 同名冲突自动追加 _N 后缀，保证 ZIP 内无重名
#   - 附带 manifest.json：file→character/scene/emotion/text 的清单，供游戏侧批量接入

import io
import json
import zipfile

from contract.models import Character, Dialogue
from contract.ports import AudioStore


def _safe(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in (name or ""))
    return cleaned.strip() or "未命名"


def build_zip(
    dialogues: list[Dialogue],
    characters_by_id: dict[str, Character],
    audio_store: AudioStore,
) -> bytes:
    buf = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = []
        for d in dialogues:
            if not d.audio_path or not audio_store.exists(d.audio_path):
                continue
            character = characters_by_id.get(d.character_id)
            char_name = character.name if character else "未知角色"
            ext = d.audio_path.rsplit(".", 1)[-1] if "." in d.audio_path else "bin"
            base = _safe(d.filename or d.id)
            folder = f"{_safe(d.scene)}/" if d.scene else ""
            arcname = f"{folder}{_safe(char_name)}/{base}.{ext}"
            n = 1
            while arcname in seen:
                arcname = f"{folder}{_safe(char_name)}/{base}_{n}.{ext}"
                n += 1
            seen.add(arcname)
            zf.writestr(arcname, audio_store.open(d.audio_path))
            manifest.append({
                "file": arcname,
                "character": char_name,
                "scene": d.scene,
                "emotion": d.emotion.value,
                "text": d.text,
            })
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()
