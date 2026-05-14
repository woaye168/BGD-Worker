# @purpose: 多格式对话批量解析（剧本/CSV/JSON/纯文本 → Dialogue 列表）
# @layer: logic
# @contract:
#   - parse_screenplay(content, resolver, default_emotion, scene) -> list[Dialogue]
#   - parse_csv(content, resolver, default_character_id, default_emotion, scene) -> list[Dialogue]
#   - parse_json(content, resolver, default_character_id, default_emotion, scene) -> list[Dialogue]
#   - parse_text(content, default_character_id, default_emotion, scene) -> list[Dialogue]
#   - normalize_emotion(text) -> Emotion
# @depends:
#   - csv, json, io, re, uuid, datetime (stdlib)
#   - typing.Callable
#   - ../contract/models.py: Dialogue, Emotion
#   - ../contract/errors.py: ValidationError
# @invariants:
#   - resolver 签名：(name: str) -> str（返回角色 id，未匹配返回空串）；是否自动建档由调用方在 resolver 内决定
#   - 解析后的 Dialogue 一律未合成（audio_path=None），scene 由参数统一赋值，由 DialogueService.bulk_add 持久化
#   - 剧本格式：每行 `角色名[(情感)]:台词`，支持中/英文冒号；无冒号非空行视为上一说话人的延续行
#   - 情感标记 (xxx)/（xxx) 可出现在角色名后或台词开头，台词开头优先级更高
#   - CSV 列顺序固定：角色名, 情感, 文本, [文件名]；少于 3 列按降级规则解析
#   - 跳过：空行、注释行(# 或 //)、空 text；中止：未解析到角色（既无 resolver 命中也无 default）

import csv
import json
import re
from datetime import datetime
from io import StringIO
from typing import Callable, Optional
from uuid import uuid4

from contract.errors import ValidationError
from contract.models import Dialogue, Emotion

_EMOTION_ALIASES = {
    "中性": Emotion.NEUTRAL, "neutral": Emotion.NEUTRAL,
    "开心": Emotion.HAPPY, "happy": Emotion.HAPPY,
    "悲伤": Emotion.SAD, "sad": Emotion.SAD,
    "愤怒": Emotion.ANGRY, "angry": Emotion.ANGRY,
    "惊讶": Emotion.SURPRISE, "surprise": Emotion.SURPRISE,
    "害怕": Emotion.FEAR, "fear": Emotion.FEAR,
    "平静": Emotion.CALM, "calm": Emotion.CALM,
}

_LEADING_EMOTION = re.compile(r"^[（(]\s*(.+?)\s*[）)]\s*")
_NAME_WITH_EMOTION = re.compile(r"^(.*?)[（(]\s*(.+?)\s*[）)]\s*$")


def normalize_emotion(text: str | None) -> Emotion:
    if not text:
        return Emotion.NEUTRAL
    return _EMOTION_ALIASES.get(text.strip().lower(), Emotion.NEUTRAL)


def _build(
    character_id: str, emotion: Emotion, text: str,
    filename: str = "", scene: str = "",
) -> Dialogue:
    return Dialogue(
        id=uuid4().hex[:10],
        character_id=character_id,
        text=text.strip(),
        emotion=emotion,
        scene=scene,
        filename=filename.strip() or None,
        created_at=datetime.utcnow(),
    )


def _require_character(character_id: str, row_repr: str) -> None:
    if not character_id:
        raise ValidationError(f"无法解析角色（既无匹配也无默认角色）：{row_repr}")


def _first_colon(line: str) -> int:
    positions = [p for p in (line.find(":"), line.find("：")) if p != -1]
    return min(positions) if positions else -1


def _extract_leading_emotion(text: str, fallback: Emotion) -> tuple[Emotion, str]:
    m = _LEADING_EMOTION.match(text)
    if m:
        emo = _EMOTION_ALIASES.get(m.group(1).strip().lower())
        if emo is not None:
            return emo, text[m.end():].strip()
    return fallback, text


def _split_name_emotion(name_part: str) -> tuple[str, Optional[Emotion]]:
    m = _NAME_WITH_EMOTION.match(name_part)
    if m:
        emo = _EMOTION_ALIASES.get(m.group(2).strip().lower())
        return m.group(1).strip(), emo
    return name_part, None


def parse_screenplay(
    content: str,
    resolver: Callable[[str], str],
    default_emotion: Emotion = Emotion.NEUTRAL,
    scene: str = "",
) -> list[Dialogue]:
    out: list[Dialogue] = []
    last_character_id = ""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        idx = _first_colon(line)
        if idx == -1:
            if last_character_id:
                emo, text = _extract_leading_emotion(line, default_emotion)
                if text:
                    out.append(_build(last_character_id, emo, text, scene=scene))
            continue
        name_part = line[:idx].strip()
        text_part = line[idx + 1:].strip()
        name, name_emo = _split_name_emotion(name_part)
        emo, text = _extract_leading_emotion(text_part, name_emo or default_emotion)
        if not name or not text:
            continue
        cid = resolver(name)
        _require_character(cid, name)
        last_character_id = cid
        out.append(_build(cid, emo, text, scene=scene))
    return out


def parse_csv(
    content: str,
    resolver: Callable[[str], str],
    default_character_id: str = "",
    default_emotion: Emotion = Emotion.NEUTRAL,
    scene: str = "",
) -> list[Dialogue]:
    out: list[Dialogue] = []
    reader = csv.reader(StringIO(content))
    for row in reader:
        cells = [c.strip() for c in row]
        if not cells or not any(cells):
            continue
        if len(cells) >= 3:
            cid = resolver(cells[0]) or default_character_id
            emo = normalize_emotion(cells[1]) if cells[1] else default_emotion
            text = cells[2]
            filename = cells[3] if len(cells) > 3 else ""
        elif len(cells) == 2:
            cid = resolver(cells[0]) or default_character_id
            emo = default_emotion
            text = cells[1]
            filename = ""
        else:
            cid = default_character_id
            emo = default_emotion
            text = cells[0]
            filename = ""
        if not text:
            continue
        _require_character(cid, repr(cells))
        out.append(_build(cid, emo, text, filename, scene))
    return out


def parse_json(
    content: str,
    resolver: Callable[[str], str],
    default_character_id: str = "",
    default_emotion: Emotion = Emotion.NEUTRAL,
    scene: str = "",
) -> list[Dialogue]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValidationError(f"JSON 解析失败：{e.msg}") from e
    if not isinstance(data, list):
        raise ValidationError("JSON 顶层必须是数组")
    out: list[Dialogue] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValidationError(f"第 {idx} 项不是对象")
        name = item.get("character") or item.get("role") or item.get("name") or ""
        cid = resolver(name) or default_character_id
        emo_raw = item.get("emotion")
        emo = normalize_emotion(emo_raw) if emo_raw else default_emotion
        text = (item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        _require_character(cid, f"item#{idx}")
        out.append(_build(cid, emo, text, item.get("filename", ""), item.get("scene", scene)))
    return out


def parse_text(
    content: str,
    default_character_id: str,
    default_emotion: Emotion = Emotion.NEUTRAL,
    scene: str = "",
) -> list[Dialogue]:
    if not default_character_id:
        raise ValidationError("纯文本格式必须指定默认角色")
    out: list[Dialogue] = []
    for line in content.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("//"):
            continue
        out.append(_build(default_character_id, default_emotion, text, scene=scene))
    return out
