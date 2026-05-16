# @purpose: 应用配置（数据目录、TTS 引擎、模型管理、AI 集成、音频格式、日志、运行时持久化设置）
# @layer: contract
# @contract:
#   - default_data_dir() -> Path
#   - LocalTTSSettings(backend, runtime_installed, runtime_version)
#   - CatalogSettings(url, cache_ttl_sec)
#   - AISettings(provider, base_url, api_key, model)   # 远期 AI 对话/情感生成预留壳
#   - TTSSettings(engine, default_voice, output_format, ffmpeg_path, rest_base_url, rest_model,
#                 local: LocalTTSSettings, catalog: CatalogSettings)
#   - LogSettings(enabled, level, to_file)
#   - AppConfig(data_dir, audio_dir_override, tts, log, ai)
#       + 派生属性 audio_dir / settings_file / log_dir / db_file / characters_file / dialogues_file
#                / models_dir / runtimes_dir / local_tts_runtime_dir
#       + load() / save() / ensure_dirs()
# @depends:
#   - pydantic
#   - json, os, sys, pathlib (stdlib)
# @invariants:
#   - data_dir 是唯一根；audio_dir/log_dir/db_file/settings_file/models_dir/runtimes_dir 全部派生
#   - audio_dir 默认 data_dir/audio；audio_dir_override 非空时优先（用户在设置页指定的自定义目录）
#   - audio_dir_override 变更只影响"下次合成"产物落盘位置；既有 audio_path 不会迁移
#   - models_dir = data_dir/models；每个模型一个子目录（id 为目录名）
#   - runtimes_dir = data_dir/runtimes；local_tts_runtime_dir = runtimes_dir/local-tts
#     运行时路径始终从 data_dir 派生，禁止持久化绝对路径（避免机器迁移失效）
#   - 冻结模式(PyInstaller)下 data_dir 落到用户家目录；NPC_VOICE_DATA_DIR 环境变量可覆盖
#   - 所有路径在 ensure_dirs() 内创建（含 log_dir，仅在 log.to_file 为真时）
#   - tts.output_format ∈ {'ogg','mp3','wav'}；'ogg'/'wav' 需 ffmpeg，引擎层负责回退
#   - tts.engine 是"默认引擎"标记，仅当 Character.voice 无前缀时被使用（dispatch 兜底）
#   - tts.local.backend ∈ {'auto','cuda','directml','cpu'}；'auto' 由 local_engine 启动期探测
#   - tts.local.runtime_installed 表示本地 TTS 运行时是否已下载安装（用户在"模型管理"页触发）
#   - tts.catalog.url 是模型目录/运行时 manifest 的 GitHub Release JSON URL；默认指向项目仓库 catalog.json（含运行时 windows_x64 元数据），用户仍可通过设置页覆盖
#   - ai.provider/api_key 当前仅占位，未在合成路径使用（Phase 3 远期接入）
#   - log.level ∈ {'debug','info','warning','error'}；变更需调 api.logging_setup.setup_logging 生效
#   - load() 不存在或损坏时回退默认值，不抛异常；data_dir 始终来自 default_data_dir()，不从文件读
#   - save() 原子写（write-temp + replace），不持久化 data_dir 字段
#   - 新增字段必须带默认值；旧 settings.json 缺字段时按默认值补全（Pydantic 默认行为）

import json
import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


def default_data_dir() -> Path:
    env = os.environ.get("NPC_VOICE_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path.home() / ".npc-voice-gen"
    return Path("./data")


class LocalTTSSettings(BaseModel):
    """本地 TTS 引擎子设置（运行时下载/后端选择）。

    runtime_installed=False 时，dispatch 收到 'local:xxx' voice 会返回友好错误，
    提示用户先在"模型管理"页安装运行时。
    """

    backend: str = "auto"  # auto|cuda|directml|cpu (Win 仅这几种；非 Win 当前不支持)
    runtime_installed: bool = False
    runtime_version: Optional[str] = None


class CatalogSettings(BaseModel):
    """在线模型 catalog 配置（GitHub Release JSON URL）。"""

    url: str = (
        "https://raw.githubusercontent.com/woaye168/BGD-Worker/main/catalog.json"
    )
    cache_ttl_sec: int = 3600


class AISettings(BaseModel):
    """AI 对话/情感生成预留壳（Phase 3 远期接入；当前未在合成路径使用）。"""

    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class TTSSettings(BaseModel):
    engine: str = "edge"
    default_voice: str = "zh-CN-XiaoxiaoNeural"
    output_format: str = "ogg"
    ffmpeg_path: Optional[str] = None
    rest_base_url: Optional[str] = None
    rest_model: Optional[str] = None
    local: LocalTTSSettings = Field(default_factory=LocalTTSSettings)
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)


class LogSettings(BaseModel):
    enabled: bool = True
    level: str = "info"
    to_file: bool = True


class AppConfig(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir)
    audio_dir_override: Optional[Path] = None
    tts: TTSSettings = Field(default_factory=TTSSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    ai: AISettings = Field(default_factory=AISettings)

    @property
    def audio_dir(self) -> Path:
        return self.audio_dir_override if self.audio_dir_override else self.data_dir / "audio"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def characters_file(self) -> Path:
        return self.data_dir / "characters.json"

    @property
    def dialogues_file(self) -> Path:
        return self.data_dir / "dialogues.json"

    @property
    def models_dir(self) -> Path:
        """模型存储根目录：每个模型一个子目录（目录名=模型 id）。"""
        return self.data_dir / "models"

    @property
    def runtimes_dir(self) -> Path:
        """运行时存储根目录：每种引擎一个子目录。"""
        return self.data_dir / "runtimes"

    @property
    def local_tts_runtime_dir(self) -> Path:
        """本地 TTS 运行时目录：runtimes/local-tts。"""
        return self.runtimes_dir / "local-tts"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.runtimes_dir.mkdir(parents=True, exist_ok=True)
        if self.log.to_file:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls) -> "AppConfig":
        instance = cls()
        if not instance.settings_file.exists():
            return instance
        try:
            raw = json.loads(instance.settings_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return instance
            raw.pop("data_dir", None)
            return cls.model_validate({**raw, "data_dir": instance.data_dir})
        except Exception:
            return instance

    def save(self) -> None:
        self.ensure_dirs()
        data = self.model_dump(mode="json", exclude={"data_dir"})
        tmp = self.settings_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.settings_file)
