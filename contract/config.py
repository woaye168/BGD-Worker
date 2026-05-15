# @purpose: 应用配置（数据目录、TTS 引擎、音频格式、日志、运行时持久化设置）
# @layer: contract
# @contract:
#   - default_data_dir() -> Path
#   - TTSSettings(engine, default_voice, output_format, ffmpeg_path, rest_base_url, rest_model)
#   - LogSettings(enabled, level, to_file)
#   - AppConfig(data_dir, audio_dir_override, tts, log)
#       + 派生属性 audio_dir / settings_file / log_dir / db_file / characters_file / dialogues_file
#       + load() / save() / ensure_dirs()
# @depends:
#   - pydantic
#   - json, os, sys, pathlib (stdlib)
# @invariants:
#   - data_dir 是唯一根；audio_dir/log_dir/db_file/settings_file 全部派生
#   - audio_dir 默认 data_dir/audio；audio_dir_override 非空时优先（用户在设置页指定的自定义目录）
#   - audio_dir_override 变更只影响"下次合成"产物落盘位置；既有 audio_path 不会迁移
#   - 冻结模式(PyInstaller)下 data_dir 落到用户家目录；NPC_VOICE_DATA_DIR 环境变量可覆盖
#   - 所有路径在 ensure_dirs() 内创建（含 log_dir，仅在 log.to_file 为真时）
#   - tts.output_format ∈ {'ogg','mp3','wav'}；'ogg'/'wav' 需 ffmpeg，引擎层负责回退
#   - log.level ∈ {'debug','info','warning','error'}；变更需调 api.logging_setup.setup_logging 生效
#   - load() 不存在或损坏时回退默认值，不抛异常；data_dir 始终来自 default_data_dir()，不从文件读
#   - save() 原子写（write-temp + replace），不持久化 data_dir 字段

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


class TTSSettings(BaseModel):
    engine: str = "edge"
    default_voice: str = "zh-CN-XiaoxiaoNeural"
    output_format: str = "ogg"
    ffmpeg_path: Optional[str] = None
    rest_base_url: Optional[str] = None
    rest_model: Optional[str] = None


class LogSettings(BaseModel):
    enabled: bool = True
    level: str = "info"
    to_file: bool = True


class AppConfig(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir)
    audio_dir_override: Optional[Path] = None
    tts: TTSSettings = Field(default_factory=TTSSettings)
    log: LogSettings = Field(default_factory=LogSettings)

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

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
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
