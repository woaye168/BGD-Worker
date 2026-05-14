# @purpose: FastAPI 依赖注入（配置/仓储/引擎/服务/编排器的单例工厂）
# @layer: adapter
# @contract:
#   - get_config() -> AppConfig
#   - get_character_repo() -> CharacterRepository
#   - get_dialogue_repo() -> DialogueRepository
#   - get_audio_store() -> AudioStore
#   - get_tts_engine() -> TTSEngine
#   - get_character_service() -> CharacterService
#   - get_dialogue_service() -> DialogueService
#   - get_orchestrator() -> SynthesisOrchestrator
# @depends:
#   - functools.lru_cache
#   - ../contract/config.py
#   - ../character/service.py, ../dialogue/service.py
#   - ../synthesis/orchestrator.py
#   - ../storage/json_repository.py, ../storage/audio_store.py
#   - ../tts/edge_tts_engine.py
# @invariants:
#   - 所有 get_* 函数返回进程单例（lru_cache 维度为无参）
#   - get_config 启动时调用 AppConfig.ensure_dirs()，保证 data_dir/audio_dir 存在
#   - TTS 引擎按 config.tts.engine 分支创建，新增引擎需在此扩展（见 AI_MAINTENANCE.md）

from functools import lru_cache

from character.service import CharacterService
from contract.config import AppConfig
from contract.errors import DomainError
from contract.ports import (
    AudioStore,
    CharacterRepository,
    DialogueRepository,
    TTSEngine,
)
from dialogue.service import DialogueService
from storage.audio_store import FileSystemAudioStore
from storage.json_repository import JSONCharacterRepository, JSONDialogueRepository
from synthesis.orchestrator import SynthesisOrchestrator
from tts.edge_tts_engine import EdgeTTSEngine


@lru_cache
def get_config() -> AppConfig:
    cfg = AppConfig()
    cfg.ensure_dirs()
    return cfg


@lru_cache
def get_character_repo() -> CharacterRepository:
    return JSONCharacterRepository(get_config().characters_file)


@lru_cache
def get_dialogue_repo() -> DialogueRepository:
    return JSONDialogueRepository(get_config().dialogues_file)


@lru_cache
def get_audio_store() -> AudioStore:
    return FileSystemAudioStore(get_config().audio_dir)


@lru_cache
def get_tts_engine() -> TTSEngine:
    cfg = get_config()
    engine = cfg.tts.engine
    if engine == "edge":
        return EdgeTTSEngine(
            output_format=cfg.tts.output_format,
            ffmpeg_path=cfg.tts.ffmpeg_path,
        )
    raise DomainError(f"未知 TTS 引擎: {engine}")


@lru_cache
def get_character_service() -> CharacterService:
    return CharacterService(get_character_repo())


@lru_cache
def get_dialogue_service() -> DialogueService:
    return DialogueService(get_dialogue_repo())


@lru_cache
def get_orchestrator() -> SynthesisOrchestrator:
    return SynthesisOrchestrator(
        tts=get_tts_engine(),
        audio_store=get_audio_store(),
        characters=get_character_repo(),
        dialogues=get_dialogue_repo(),
    )
