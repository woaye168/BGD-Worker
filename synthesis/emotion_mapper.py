# @purpose: 情感到声学参数 / SSML style 的映射表
# @layer: logic
# @contract:
#   - EMOTION_TO_PROSODY: dict[Emotion, dict] (rate_mul/pitch_offset_pct/volume_mul)
#   - EMOTION_TO_AZURE_STYLE: dict[Emotion, tuple[str, float]] (style_name, degree)
#   - get_prosody(emotion) -> dict
#   - get_azure_style(emotion) -> tuple[str, float]
# @depends:
#   - ../contract/models.py: Emotion
# @invariants:
#   - 两张表必须覆盖 Emotion 枚举所有成员，否则 KeyError
#   - rate_mul / volume_mul 是相对角色基础参数的乘子；pitch_offset_pct 是百分比偏移
#   - 新增情感时必须同步两表（见 AI_MAINTENANCE.md 扩展指南）

from contract.models import Emotion

EMOTION_TO_PROSODY: dict[Emotion, dict] = {
    Emotion.NEUTRAL:  {"rate_mul": 1.00, "pitch_offset_pct":   0, "volume_mul": 1.00},
    Emotion.HAPPY:    {"rate_mul": 1.10, "pitch_offset_pct":  15, "volume_mul": 1.00},
    Emotion.SAD:      {"rate_mul": 0.85, "pitch_offset_pct": -15, "volume_mul": 0.90},
    Emotion.ANGRY:    {"rate_mul": 1.18, "pitch_offset_pct":   5, "volume_mul": 1.00},
    Emotion.SURPRISE: {"rate_mul": 1.20, "pitch_offset_pct":  30, "volume_mul": 1.00},
    Emotion.FEAR:     {"rate_mul": 1.05, "pitch_offset_pct":  20, "volume_mul": 0.85},
    Emotion.CALM:     {"rate_mul": 0.92, "pitch_offset_pct":  -5, "volume_mul": 0.95},
}

EMOTION_TO_AZURE_STYLE: dict[Emotion, tuple[str, float]] = {
    Emotion.NEUTRAL:  ("general", 1.0),
    Emotion.HAPPY:    ("cheerful", 1.0),
    Emotion.SAD:      ("sad", 1.0),
    Emotion.ANGRY:    ("angry", 1.0),
    Emotion.SURPRISE: ("excited", 1.0),
    Emotion.FEAR:     ("fearful", 1.0),
    Emotion.CALM:     ("calm", 1.0),
}


def get_prosody(emotion: Emotion) -> dict:
    return EMOTION_TO_PROSODY[emotion]


def get_azure_style(emotion: Emotion) -> tuple[str, float]:
    return EMOTION_TO_AZURE_STYLE[emotion]
