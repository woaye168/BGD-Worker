# @purpose: 跨层端口协议（TTS 引擎、仓储、模型管理）
# @layer: contract
# @contract:
#   - TTSEngine.{output_extension, synthesize, list_voices}
#   - CharacterRepository.{list, get, upsert, delete}
#   - DialogueRepository.{list, get, upsert, delete, bulk_add, reorder}
#   - AudioStore.{save, open, exists, delete, absolute}
#   - ModelStore.{list_installed, get, import_from_path, remove, root}
#   - ModelCatalog.{fetch, download}
#   - RuntimeInstaller.{name, is_installed, installed_version, install, uninstall}
# @depends:
#   - typing (Protocol, AsyncIterator)
#   - pathlib
#   - ./models.py
# @invariants:
#   - 所有 Protocol 都是 runtime_checkable，便于在 deps 注入时校验
#   - TTSEngine.synthesize 返回完整音频字节流（含容器头），格式由 output_extension 声明
#   - TTSEngine.output_extension 取值与 TTSSettings.output_format 一致（ogg/mp3/wav），编排层据此命名文件
#   - TTSEngine.voice 入参允许携带 "engine:" 前缀；分发引擎负责剥前缀再传给具体子引擎
#   - AudioStore.save 返回的字符串是相对 audio_dir.parent 的可读路径，可直接持久化到 Dialogue.audio_path
#   - DialogueRepository.reorder 的 ids 必须是仓储内全部对话 id 的一个排列；不匹配时实现应抛 StorageError
#   - 仓储实现可同步（当前 JSON 文件实现即同步），编排层不假定异步
#   - ModelStore 是"已安装本地模型"的视图；catalog（在线目录）由 ModelCatalog 单独提供
#   - ModelCatalog.fetch 应实现 cache_ttl 缓存；force=True 强制刷新；失败抛 ModelError
#   - ModelCatalog.download / RuntimeInstaller.install 以 AsyncIterator[dict] 流式输出进度事件
#     约定事件形状: {"phase": str, "percent": float?, "message": str?, "model_id"?: str}
#     phase ∈ {"start","downloading","verifying","extracting","done","error"}

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

from .models import Character, Dialogue, Emotion, TTSModel


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
    def reorder(self, ids: list[str]) -> None: ...


@runtime_checkable
class AudioStore(Protocol):
    def save(self, dialogue_id: str, filename: str, data: bytes) -> str: ...
    def open(self, audio_path: str) -> bytes: ...
    def exists(self, audio_path: str) -> bool: ...
    def delete(self, audio_path: str) -> bool: ...
    def absolute(self, audio_path: str) -> Path: ...


@runtime_checkable
class ModelStore(Protocol):
    """已安装本地 TTS 模型的视图（扫描 data_dir/models/<id>/）。"""

    def list_installed(self) -> list[TTSModel]: ...
    def get(self, id: str) -> Optional[TTSModel]: ...
    def import_from_path(self, path: Path) -> TTSModel: ...
    def remove(self, id: str) -> bool: ...
    def root(self) -> Path: ...


@runtime_checkable
class ModelCatalog(Protocol):
    """在线模型目录（GitHub Release 上的 catalog.json + 模型 zip）。"""

    async def fetch(self, force: bool = False) -> list[TTSModel]: ...
    def download(self, id: str) -> AsyncIterator[dict]: ...


@runtime_checkable
class RuntimeInstaller(Protocol):
    """按需安装/卸载 TTS 推理运行时（如本地 GPT-SoVITS）。

    单个引擎一个实现；与 ModelStore 解耦（运行时是"引擎"，模型是"角色音色"）。
    """

    @property
    def name(self) -> str: ...

    def is_installed(self) -> bool: ...
    def installed_version(self) -> Optional[str]: ...
    def install(self) -> AsyncIterator[dict]: ...
    def uninstall(self) -> bool: ...
