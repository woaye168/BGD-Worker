# @purpose: 多格式对话批量解析（CSV/JSON/纯文本 → Dialogue 列表）
# @layer: logic
# @contract:
#   - parse_csv(content, resolver, default_character_id, default_emotion) -> list[Dialogue]
#   - parse_json(content, resolver, default_character_id, default_emotion) -> list[Dialogue]
#   - parse_text(content, default_character_id, default_emotion) -> list[Dialogue]
#   - normalize_emotion(text) -> Emotion
# @depends:
#   - csv, json, io, uuid, datetime (stdlib)
#   - typing.Callable
#   - ../contract/models.py: Dialogue, Emotion
#   - ../contract/errors.py: ValidationError
# @invariants:
#   - resolver 签名：(name: str) -> str（返回角色 id，未匹配返回空串）
#   - 解析后的 Dialogue 一律未合成（audio_path=None），由 DialogueService.bulk_add 持久化
#   - CSV 列顺序固定：角色名, 情感, 文本, [文件名]；少于 3 列按降级规则解析
#   - 跳过：空行、空 text；中止：未解析到角色（既无 resolver 命中也无 default）

import csv
import json
from datetime import datetime
from io import StringIO
from typing import Callable
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


def normalize_emotion(text: str | None) -> Emotion:
    if not text:
        return Emotion.NEUTRAL
    return _EMOTION_ALIASES.get(text.strip().lower(), Emotion.NEUTRAL)


def _build(character_id: str, emotion: Emotion, text: str, filename: str = "") -> Dialogue:
    return Dialogue(
        id=uuid4().hex[:10],
        character_id=character_id,
        text=text.strip(),
        emotion=emotion,
        filename=filename.strip() or None,
        created_at=datetime.utcnow(),
    )


def _require_character(character_id: str, row_repr: str) -> None:
    if not character_id:
        raise ValidationError(f"无法解析角色（既无匹配也无默认角色）：{row_repr}")


def parse_csv(
    content: str,
    resolver: Callable[[str], str],
    default_character_id: str = "",
    default_emotion: Emotion = Emotion.NEUTRAL,
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
        out.append(_build(cid, emo, text, filename))
    return out


def parse_json(
    content: str,
    resolver: Callable[[str], str],
    default_character_id: str = "",
    default_emotion: Emotion = Emotion.NEUTRAL,
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
        out.append(_build(cid, emo, text, item.get("filename", "")))
    return out


def parse_text(
    content: str,
    default_character_id: str,
    default_emotion: Emotion = Emotion.NEUTRAL,
) -> list[Dialogue]:
    if not default_character_id:
        raise ValidationError("纯文本格式必须指定默认角色")
    out: list[Dialogue] = []
    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        out.append(_build(default_character_id, default_emotion, text))
    return out
