# @purpose: 应用配置（数据目录、TTS 引擎与音频格式设置）
# @layer: contract
# @contract:
#   - default_data_dir() -> Path
#   - TTSSettings(engine, default_voice, output_format, ffmpeg_path, rest_base_url, rest_model)
#   - AppConfig(data_dir, tts) + 派生属性 audio_dir/characters_file/dialogues_file
# @depends:
#   - pydantic
#   - os, sys, pathlib (stdlib)
# @invariants:
#   - data_dir 是唯一根：audio_dir/characters_file/dialogues_file 全部由它派生，相对位置固定
#   - 冻结模式(PyInstaller)下 data_dir 落到用户家目录，开发模式落到 ./data；NPC_VOICE_DATA_DIR 环境变量可覆盖
#   - 所有路径在 AppConfig.ensure_dirs() 内确保存在，调用方需在启动时调用一次
#   - tts.output_format 取值 'ogg' | 'mp3' | 'wav'；'ogg'/'wav' 需 ffmpeg，引擎层负责无 ffmpeg 时回退
#   - tts.ffmpeg_path 为 None 表示"自动探测"（系统 PATH → imageio-ffmpeg）
#   - tts.engine 取值 'edge' | 'rest'，新增引擎需同步更新 api.deps:get_tts_engine

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


class AppConfig(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir)
    tts: TTSSettings = Field(default_factory=TTSSettings)

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def characters_file(self) -> Path:
        return self.data_dir / "characters.json"

    @property
    def dialogues_file(self) -> Path:
        return self.data_dir / "dialogues.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
