# @purpose: 领域模型（角色、对话、情感、合成任务、TTS 模型元数据）
# @layer: contract
# @contract:
#   - Emotion: Enum (neutral/happy/sad/angry/surprise/fear/calm)
#   - Character(id, name, voice, rate, pitch, volume, default_emotion)
#   - Dialogue(id, character_id, text, emotion, scene, filename, created_at, audio_path, synthesized_at)
#   - SynthesisScope: Enum (pending/all/selected)
#   - SynthesisRequest(scope, dialogue_ids)
#   - SynthesisResult(dialogue_id, success, audio_path, error)
#   - TTSModel(id, engine, name, description, character, language, source, installed,
#             size_bytes, license, license_url, download_url, sha256, files)
# @depends:
#   - pydantic (BaseModel, Field)
#   - datetime (stdlib)
#   - enum (stdlib)
# @invariants:
#   - 所有模型为贫血领域模型，不含业务行为方法
#   - Dialogue.audio_path 为空 ⇔ 该对话从未合成 / 合成已失效
#   - Dialogue.scene 是自由文本分组标签（场景/对话组），空串表示未分组；导入时由调用方赋值
#   - Character.rate/pitch/volume 不约束硬上下限，由 TTS 引擎层裁剪
#   - 字符串 id 由 service 层生成（uuid4 hex 前 10 位），contract 层不规定
#   - Character.voice 字符串语义：可含 "engine:model_id" 前缀；无前缀视为 "edge:<value>"（向后兼容）
#     具体前缀解析在 tts/dispatch_engine 层完成，contract 不强制校验
#   - TTSModel.id 在 (engine + 本地安装目录) 内唯一；不同引擎之间允许同 id 共存（dispatch 用 engine 区分）
#   - TTSModel.source ∈ {builtin, catalog, imported}；installed=True 表示文件已落盘 data_dir/models/
#   - TTSModel.download_url/sha256 仅 catalog 项有；installed 项可为空

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
    scene: str = ""
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


class TTSModel(BaseModel):
    """TTS 音色模型元数据（已安装/在线 catalog 共用一个类型）。"""

    id: str
    engine: str = "edge"
    name: str = ""
    description: str = ""
    character: str = ""
    language: str = "zh-CN"
    source: str = "builtin"
    installed: bool = False
    size_bytes: int = 0
    license: str = ""
    license_url: str = ""
    download_url: str = ""
    sha256: str = ""
    files: list[str] = Field(default_factory=list)
