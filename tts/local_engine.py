# @purpose: 本地 TTS 引擎适配（脚手架；真实 GPT-SoVITS 推理由独立运行时包提供）
# @layer: adapter
# @contract:
#   - LocalTTSEngine(runtime_dir, model_store, output_format, ffmpeg_path, backend).{
#       output_extension, synthesize, list_voices}
# @depends:
#   - logging, sys (stdlib)
#   - pathlib
#   - ../contract/models.py: Emotion, TTSModel
#   - ../contract/errors.py: TTSError
#   - ../contract/ports.py: ModelStore
#   - ./_ffmpeg.py: resolve_ffmpeg
# @invariants:
#   - 平台门禁：sys.platform != 'win32' 时 synthesize 抛 TTSError("暂不支持")；
#     list_voices 不抛（允许任意平台浏览/管理模型）
#   - 运行时门禁：runtime_dir/VERSION 不存在 → synthesize 抛 TTSError 提示用户去「模型管理」装运行时
#   - 模型门禁：voice 在 ModelStore 中不存在 → synthesize 抛 TTSError 提示下载/导入
#   - 当前实现是脚手架：通过三道门禁后立即抛 TTSError("脚手架阶段，推理待接入")
#     真实推理逻辑由独立运行时包（data_dir/runtimes/local-tts/）+ 后续版本接入
#   - output_extension：local 引擎实际产 wav；需 ogg 时由 _ffmpeg.transcode 转码（缺 ffmpeg 时降级 wav）
#   - voice 入参假定已被 dispatch 层剥掉 "local:" 前缀（本引擎只认裸 model_id）

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from contract.errors import TTSError
from contract.models import Emotion
from contract.ports import ModelStore

from ._ffmpeg import resolve_ffmpeg

logger = logging.getLogger(__name__)

_VERSION_FILE = "VERSION"


class LocalTTSEngine:
    def __init__(
        self,
        runtime_dir: Path,
        model_store: ModelStore,
        output_format: str = "ogg",
        ffmpeg_path: Optional[str] = None,
        backend: str = "auto",
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._models = model_store
        self._ffmpeg = resolve_ffmpeg(ffmpeg_path)
        requested = output_format
        fmt = output_format if output_format in ("ogg", "mp3", "wav") else "wav"
        if fmt == "ogg" and self._ffmpeg is None:
            fmt = "wav"  # local 原生产 wav；缺 ffmpeg 时无法转 ogg，降级到 wav
        elif fmt == "mp3":
            # local 无 mp3 编码器；与 edge 不一致时仅当 single-engine 配置才走 mp3
            # 此场景下退到 wav 保持可用
            fmt = "wav"
        self._format = fmt
        self._backend = backend
        logger.info(
            "local_tts engine: requested=%s effective=%s backend=%s runtime=%s ffmpeg=%s",
            requested, self._format, self._backend, self._runtime_dir,
            self._ffmpeg or "<none>",
        )

    @property
    def output_extension(self) -> str:
        return self._format

    async def list_voices(self) -> list[dict]:
        """列出已安装的本地模型作为可选 voice；不依赖运行时是否安装。"""
        installed = self._models.list_installed()
        return [
            {
                "id": m.id,
                "name": m.name or m.id,
                "lang": m.language,
                "character": m.character,
                "description": m.description,
                "license": m.license,
                "license_url": m.license_url,
            }
            for m in installed
        ]

    async def synthesize(
        self,
        text: str,
        voice: str,
        emotion: Emotion,
        rate: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
    ) -> bytes:
        # 1. 平台门禁
        if sys.platform != "win32":
            raise TTSError(
                f"本地 TTS 引擎当前仅支持 Windows，检测到平台 {sys.platform}（暂不支持）"
            )
        # 2. 运行时门禁
        if not (self._runtime_dir / _VERSION_FILE).exists():
            raise TTSError(
                "本地 TTS 运行时未安装。请到「模型管理」页点击「安装本地 TTS 引擎」"
            )
        # 3. 模型门禁
        if not voice:
            raise TTSError("voice 为空")
        if not text.strip():
            raise TTSError("文本为空")
        model = self._models.get(voice)
        if model is None:
            raise TTSError(
                f"未找到本地模型: {voice}。请到「模型管理」页下载或导入对应模型"
            )
        # 4. 脚手架：真实推理待接入
        # 真正实现将启动 runtime 子进程，传入 model、text、emotion-derived 参数，
        # 收 WAV stdout，必要时经 _ffmpeg.transcode 转 ogg。
        raise TTSError(
            "本地 TTS 推理尚未实现（脚手架阶段）；运行时与模型已就位，请等待后续版本接入 GPT-SoVITS"
        )
