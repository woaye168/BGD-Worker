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
#   - invalidate_caches() -> None    # 清空全部 lru_cache，用于设置变更后
# @depends:
#   - functools.lru_cache
#   - ../contract/config.py
#   - ../character/service.py, ../dialogue/service.py
#   - ../synthesis/orchestrator.py
#   - ../storage/sqlite_repository.py, ../storage/audio_store.py
#   - ../tts/edge_tts_engine.py
# @invariants:
#   - 所有 get_* 函数返回进程单例（lru_cache 维度为无参）
#   - get_config 调用 AppConfig.load() 优先读取 settings.json，并 ensure_dirs()
#   - SQLite 仓储首次构造时自动建表 + 一次性迁移旧 JSON（见 sqlite_repository._migrate_json_if_needed）
#   - TTS 引擎按 config.tts.engine 分支创建，新增引擎需在此扩展（见 AI_MAINTENANCE.md）
#   - 设置变更（如 audio_dir_override）后需调 invalidate_caches() 让单例重建

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
from storage.sqlite_repository import SQLiteCharacterRepository, SQLiteDialogueRepository
from synthesis.orchestrator import SynthesisOrchestrator
from tts.edge_tts_engine import EdgeTTSEngine


@lru_cache
def get_config() -> AppConfig:
    cfg = AppConfig.load()
    cfg.ensure_dirs()
    return cfg


@lru_cache
def get_character_repo() -> CharacterRepository:
    cfg = get_config()
    return SQLiteCharacterRepository(cfg.db_file, cfg.data_dir)


@lru_cache
def get_dialogue_repo() -> DialogueRepository:
    cfg = get_config()
    return SQLiteDialogueRepository(cfg.db_file, cfg.data_dir)


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


def invalidate_caches() -> None:
    """设置变更后清空所有 lru_cache 单例，强制下次访问按新配置重建。"""
    for fn in (
        get_config,
        get_character_repo,
        get_dialogue_repo,
        get_audio_store,
        get_tts_engine,
        get_character_service,
        get_dialogue_service,
        get_orchestrator,
    ):
        fn.cache_clear()
