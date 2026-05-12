# @purpose: 领域模型（角色、对话、情感、合成任务）
# @layer: contract
# @contract:
#   - Emotion: Enum (neutral/happy/sad/angry/surprise/fear/calm)
#   - Character(id, name, voice, rate, pitch, volume, default_emotion)
#   - Dialogue(id, character_id, text, emotion, filename, created_at, audio_path, synthesized_at)
#   - SynthesisScope: Enum (pending/all/selected)
#   - SynthesisRequest(scope, dialogue_ids)
#   - SynthesisResult(dialogue_id, success, audio_path, error)
# @depends:
#   - pydantic (BaseModel, Field)
#   - datetime (stdlib)
#   - enum (stdlib)
# @invariants:
#   - 所有模型为贫血领域模型，不含业务行为方法
#   - Dialogue.audio_path 为空 ⇔ 该对话从未合成 / 合成已失效
#   - Character.rate/pitch/volume 不约束硬上下限，由 TTS 引擎层裁剪
#   - 字符串 id 由 service 层生成（uuid4 hex 前 10 位），contract 层不规定

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISE = "surprise"
    FEAR = "fear"
    CALM = "calm"


class Character(BaseModel):
    id: str
    name: str
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    default_emotion: Emotion = Emotion.NEUTRAL


class Dialogue(BaseModel):
    id: str
    character_id: str
    text: str
    emotion: Emotion = Emotion.NEUTRAL
    filename: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    audio_path: Optional[str] = None
    synthesized_at: Optional[datetime] = None


class SynthesisScope(str, Enum):
    PENDING = "pending"
    ALL = "all"
    SELECTED = "selected"


class SynthesisRequest(BaseModel):
    scope: SynthesisScope = SynthesisScope.PENDING
    dialogue_ids: list[str] = Field(default_factory=list)


class SynthesisResult(BaseModel):
    dialogue_id: str
    success: bool
    audio_path: Optional[str] = None
    error: Optional[str] = None
