# @purpose: 应用配置（路径、TTS 引擎设置）
# @layer: contract
# @contract:
#   - TTSSettings(engine, default_voice, ffmpeg_path, rest_base_url, rest_model)
#   - AppConfig(data_dir, audio_dir, characters_file, dialogues_file, tts)
# @depends:
#   - pydantic
#   - pathlib (stdlib)
# @invariants:
#   - 配置值仅声明默认，实际值由环境变量或调用方覆盖
#   - 所有路径在 AppConfig.ensure_dirs() 内确保存在，调用方需在启动时调用一次
#   - tts.engine 取值范围：'edge' | 'rest' ，新增引擎需同步更新 api.deps:get_tts_engine

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class TTSSettings(BaseModel):
    engine: str = "edge"
    default_voice: str = "zh-CN-XiaoxiaoNeural"
    ffmpeg_path: str = "ffmpeg"
    rest_base_url: Optional[str] = None
    rest_model: Optional[str] = None


class AppConfig(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path("./data"))
    audio_dir: Path = Field(default_factory=lambda: Path("./data/audio"))
    characters_file: Path = Field(default_factory=lambda: Path("./data/characters.json"))
    dialogues_file: Path = Field(default_factory=lambda: Path("./data/dialogues.json"))
    tts: TTSSettings = Field(default_factory=TTSSettings)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
