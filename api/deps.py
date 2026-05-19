# @purpose: FastAPI 依赖注入（配置/仓储/引擎/服务/编排器/模型管理的单例工厂）
# @layer: adapter
# @contract:
#   - get_config() -> AppConfig
#   - get_character_repo() -> CharacterRepository
#   - get_dialogue_repo() -> DialogueRepository
#   - get_audio_store() -> AudioStore
#   - get_model_store() -> ModelStore
#   - get_catalog() -> ModelCatalog
#   - get_runtime_installer() -> RuntimeInstaller
#   - get_tts_engine() -> TTSEngine    # 返回 DispatchTTSEngine, 内部含 edge+local 子引擎
#   - get_character_service() -> CharacterService
#   - get_dialogue_service() -> DialogueService
#   - get_orchestrator() -> SynthesisOrchestrator
#   - invalidate_caches() -> None    # 清空全部 lru_cache，用于设置/模型变更后
# @depends:
#   - functools.lru_cache
#   - ../contract/config.py, ../contract/ports.py
#   - ../character/service.py, ../dialogue/service.py
#   - ../synthesis/orchestrator.py
#   - ../storage/sqlite_repository.py, ../storage/audio_store.py, ../storage/model_store.py
#   - ../tts/edge_tts_engine.py, ../tts/local_engine.py, ../tts/dispatch_engine.py
#   - ../tts/catalog_client.py, ../tts/runtime_installer.py
# @invariants:
#   - 所有 get_* 函数返回进程单例（lru_cache 维度为无参）
#   - get_config 调用 AppConfig.load() 优先读取 settings.json，并 ensure_dirs()
#   - SQLite 仓储首次构造时自动建表 + 一次性迁移旧 JSON（见 sqlite_repository._migrate_json_if_needed）
#   - get_tts_engine 始终返回 DispatchTTSEngine（含 edge 兜底 + local 脚手架）；
#     旧 cfg.tts.engine 字段当前仅作展示意义，实际引擎选择由 Character.voice 前缀决定
#   - get_catalog 与 get_runtime_installer 共用 cfg.tts.catalog.url 作为同源 JSON
#     (catalog 读取 .models[]，installer 读取 .windows_x64{})
#   - 模型导入/删除/下载/运行时安装卸载 后必须调 invalidate_caches() 让 dispatch 与 voice 列表重建

from functools import lru_cache

from character.service import CharacterService
from contract.config import AppConfig
from contract.ports import (
    AudioStore,
    CharacterRepository,
    DialogueRepository,
    ModelCatalog,
    ModelStore,
    RuntimeInstaller,
    TTSEngine,
)
from dialogue.service import DialogueService
from storage.audio_store import FileSystemAudioStore
from storage.model_store import FileSystemModelStore
from storage.sqlite_repository import SQLiteCharacterRepository, SQLiteDialogueRepository
from synthesis.orchestrator import SynthesisOrchestrator
from tts.catalog_client import GithubReleaseCatalog
from tts.dispatch_engine import DispatchTTSEngine
from tts.edge_tts_engine import EdgeTTSEngine
from tts.local_engine import LocalTTSEngine
from tts.runtime_installer import LocalTTSRuntimeInstaller, _manifest_slot_key


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
def get_model_store() -> ModelStore:
    return FileSystemModelStore(get_config().models_dir)


@lru_cache
def get_catalog() -> ModelCatalog:
    cfg = get_config()
    return GithubReleaseCatalog(
        url=cfg.tts.catalog.url,
        models_dir=cfg.models_dir,
        cache_ttl_sec=cfg.tts.catalog.cache_ttl_sec,
    )


@lru_cache
def get_runtime_installer() -> RuntimeInstaller:
    cfg = get_config()
    return _make_runtime_installer(cfg.tts.local.target)


def _make_runtime_installer(target: str) -> RuntimeInstaller:
    """为指定 target 创建 installer（不缓存，因为 target 是变参）。

    用于 /runtime/status 查询所有 target 状态时按需实例化。
    """
    cfg = get_config()
    return LocalTTSRuntimeInstaller(
        install_dir=cfg.local_tts_runtime_dir(target),
        manifest_url=cfg.tts.catalog.url,
        target=target,
    )


@lru_cache
def get_tts_engine() -> TTSEngine:
    cfg = get_config()
    edge = EdgeTTSEngine(
        output_format=cfg.tts.output_format,
        ffmpeg_path=cfg.tts.ffmpeg_path,
    )
    local = LocalTTSEngine(
        runtime_dir=cfg.active_local_tts_runtime_dir,
        model_store=get_model_store(),
        output_format=cfg.tts.output_format,
        ffmpeg_path=cfg.tts.ffmpeg_path,
        target=cfg.tts.local.target,
        synthesize_timeout_sec=cfg.tts.local.synthesize_timeout_sec,
        log_dir=cfg.log_dir,
        sample_steps=cfg.tts.local.sample_steps,
    )
    return DispatchTTSEngine(sub_engines={"edge": edge, "local": local})


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
    """设置/模型变更后清空所有 lru_cache 单例，强制下次访问按新状态重建。

    先尽力 close 当前 tts engine（关闭本地 TTS runtime 子进程等），再 clear lru_cache。
    """
    # 仅当 lru_cache 已有缓存时才取（避免空 clear 副作用：构造一个新实例又丢弃）
    if get_tts_engine.cache_info().currsize > 0:
        try:
            close = getattr(get_tts_engine(), "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    for fn in (
        get_config,
        get_character_repo,
        get_dialogue_repo,
        get_audio_store,
        get_model_store,
        get_catalog,
        get_runtime_installer,
        get_tts_engine,
        get_character_service,
        get_dialogue_service,
        get_orchestrator,
    ):
        fn.cache_clear()
