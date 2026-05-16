# @purpose: 多引擎分发 TTS（按 Character.voice 的 "engine:voice_id" 前缀分发到子引擎）
# @layer: adapter
# @contract:
#   - DispatchTTSEngine(sub_engines).{output_extension, synthesize, list_voices, close}
#   - parse_voice(voice) -> (engine, raw_voice)   # 模块级工具，供 routes 层复用
# @depends:
#   - logging (stdlib)
#   - ../contract/models.py: Emotion
#   - ../contract/errors.py: TTSError
#   - ../contract/ports.py: TTSEngine
# @invariants:
#   - sub_engines 字典必须含 "edge" 键作为兜底（构造时校验，否则抛 ValueError）
#   - parse_voice 规则:
#       voice=""/None → ("edge", "zh-CN-XiaoxiaoNeural")
#       voice 含 ":" → 按第一个 ":" 切分为 (engine, raw)
#       voice 不含 ":" → ("edge", voice)   # 向后兼容存量裸 voice id
#   - synthesize 时若 engine 前缀未注册到 sub_engines → 抛 TTSError("未知引擎前缀")
#   - list_voices 聚合所有子引擎产出，每条加 engine 字段；任一子引擎报错只 warning 不打断
#     聚合结果每条加 full_id 字段（"engine:id" 标准化形式），供前端写回 Character.voice 时用
#   - output_extension 返回 "edge" 子引擎的实际格式（兜底引擎；用户 settings.tts.output_format
#     应让所有子引擎一致，否则跨引擎时落盘扩展名可能与 bytes 实际格式不符）
#     ↑ 已知限制：缺 ffmpeg + 多引擎时格式可能漂移；Phase 2 处理
#   - close() 转发到所有定义了 close() 的子引擎（duck typing），用于 invalidate_caches 前清理
#     子进程等长驻资源；幂等，异常仅 warning 不打断

from __future__ import annotations

import logging
from typing import Optional

from contract.errors import TTSError
from contract.models import Emotion
from contract.ports import TTSEngine

logger = logging.getLogger(__name__)


def parse_voice(voice: Optional[str]) -> tuple[str, str]:
    """解析 'engine:raw' 形式的 voice 字符串，返回 (engine, raw)。

    无前缀视为 edge（向后兼容存量数据）；空值返回 ("edge", 默认音色)。
    """
    if not voice:
        return "edge", "zh-CN-XiaoxiaoNeural"
    if ":" in voice:
        engine, raw = voice.split(":", 1)
        return engine, raw
    return "edge", voice


class DispatchTTSEngine:
    def __init__(self, sub_engines: dict[str, TTSEngine]) -> None:
        if "edge" not in sub_engines:
            raise ValueError("DispatchTTSEngine 必须含 'edge' 子引擎作为兜底")
        self._sub = dict(sub_engines)
        engines_summary = ", ".join(self._sub.keys())
        logger.info("dispatch engine: sub-engines=%s", engines_summary)

    @property
    def output_extension(self) -> str:
        return self._sub["edge"].output_extension

    async def list_voices(self) -> list[dict]:
        result: list[dict] = []
        for engine_name, sub in self._sub.items():
            try:
                voices = await sub.list_voices()
            except Exception as e:
                logger.warning("子引擎 %s list_voices 失败: %s", engine_name, e)
                continue
            for v in voices:
                v_copy = dict(v)
                v_copy["engine"] = engine_name
                raw_id = str(v_copy.get("id") or "")
                v_copy["full_id"] = f"{engine_name}:{raw_id}" if raw_id else ""
                result.append(v_copy)
        return result

    async def synthesize(
        self,
        text: str,
        voice: str,
        emotion: Emotion,
        rate: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
    ) -> bytes:
        engine_name, raw_voice = parse_voice(voice)
        sub = self._sub.get(engine_name)
        if sub is None:
            raise TTSError(f"未知引擎前缀 '{engine_name}'，已注册: {list(self._sub.keys())}")
        return await sub.synthesize(text, raw_voice, emotion, rate, pitch, volume)

    def close(self) -> None:
        """转发到所有有 close() 的子引擎；幂等，异常忽略。"""
        for engine_name, sub in self._sub.items():
            close = getattr(sub, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as e:
                logger.warning("子引擎 %s close 失败: %s", engine_name, e)
