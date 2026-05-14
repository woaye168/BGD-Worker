# @purpose: 跨层端口协议（TTS 引擎与仓储接口）
# @layer: contract
# @contract:
#   - TTSEngine.{output_extension, synthesize, list_voices}
#   - CharacterRepository.{list, get, upsert, delete}
#   - DialogueRepository.{list, get, upsert, delete, bulk_add}
#   - AudioStore.{save, open, exists, delete, absolute}
# @depends:
#   - typing (Protocol)
#   - pathlib
#   - ./models.py
# @invariants:
#   - 所有 Protocol 都是 runtime_checkable，便于在 deps 注入时校验
#   - TTSEngine.synthesize 返回完整音频字节流（含容器头），格式由 output_extension 声明
#   - TTSEngine.output_extension 取值与 TTSSettings.output_format 一致（ogg/mp3/wav），编排层据此命名文件
#   - AudioStore.save 返回的字符串是相对 audio_dir.parent 的可读路径，可直接持久化到 Dialogue.audio_path
#   - 仓储实现可同步（当前 JSON 文件实现即同步），编排层不假定异步

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from .models import Character, Dialogue, Emotion


@runtime_checkable
class TTSEngine(Protocol):
    @property
    def output_extension(self) -> str: ...

    async def synthesize(
        self,
        text: str,
        voice: str,
        emotion: Emotion,
        rate: float,
        pitch: float,
        volume: float,
    ) -> bytes: ...

    async def list_voices(self) -> list[dict]: ...


@runtime_checkable
class CharacterRepository(Protocol):
    def list(self) -> list[Character]: ...
    def get(self, id: str) -> Optional[Character]: ...
    def upsert(self, character: Character) -> None: ...
    def delete(self, id: str) -> bool: ...


@runtime_checkable
class DialogueRepository(Protocol):
    def list(self) -> list[Dialogue]: ...
    def get(self, id: str) -> Optional[Dialogue]: ...
    def upsert(self, dialogue: Dialogue) -> None: ...
    def delete(self, id: str) -> bool: ...
    def bulk_add(self, dialogues: list[Dialogue]) -> None: ...


@runtime_checkable
class AudioStore(Protocol):
    def save(self, dialogue_id: str, filename: str, data: bytes) -> str: ...
    def open(self, audio_path: str) -> bytes: ...
    def exists(self, audio_path: str) -> bool: ...
    def delete(self, audio_path: str) -> bool: ...
    def absolute(self, audio_path: str) -> Path: ...
