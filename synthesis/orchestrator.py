# @purpose: 批量合成编排（范围筛选 → 调用 TTS → 持久化音频 → 回写仓储）
# @layer: logic
# @contract:
#   - SynthesisOrchestrator.{select, render, synthesize_one, batch}
# @depends:
#   - typing (AsyncIterator)
#   - ../contract/models.py: Dialogue, SynthesisScope, SynthesisResult
#   - ../contract/ports.py: TTSEngine, AudioStore, CharacterRepository, DialogueRepository
#   - ../contract/errors.py: NotFoundError
# @invariants:
#   - render(dialogue) 不持久化：仅调用 TTS 引擎返回音频字节，供"试听未合成对话"使用
#   - synthesize_one(dialogue) 必持久化：写 AudioStore 并通过 DialogueRepository.upsert 设置 audio_path
#   - 落盘文件扩展名取自 TTSEngine.output_extension（不再硬编码 .ogg）
#   - select(PENDING) 仅返回 audio_path 为空的对话（即"从新增对话开始"的语义来源）
#   - batch 顺序处理，逐条 yield 结果；单条失败不影响后续；调用方需自行收集
#   - 失败的合成不写入 audio_path，下次 PENDING 范围会自动重试

from datetime import datetime
from typing import AsyncIterator, Optional

from contract.errors import NotFoundError
from contract.models import Dialogue, SynthesisResult, SynthesisScope
from contract.ports import (
    AudioStore,
    CharacterRepository,
    DialogueRepository,
    TTSEngine,
)


class SynthesisOrchestrator:
    def __init__(
        self,
        tts: TTSEngine,
        audio_store: AudioStore,
        characters: CharacterRepository,
        dialogues: DialogueRepository,
    ) -> None:
        self._tts = tts
        self._audio = audio_store
        self._chars = characters
        self._dialogs = dialogues

    def select(self, scope: SynthesisScope, ids: Optional[list[str]] = None) -> list[Dialogue]:
        all_dialogs = self._dialogs.list()
        if scope is SynthesisScope.ALL:
            return all_dialogs
        if scope is SynthesisScope.SELECTED:
            idset = set(ids or [])
            return [d for d in all_dialogs if d.id in idset]
        return [d for d in all_dialogs if not d.audio_path]

    async def render(self, dialogue: Dialogue) -> bytes:
        character = self._chars.get(dialogue.character_id)
        if character is None:
            raise NotFoundError(f"character not found: {dialogue.character_id}")
        return await self._tts.synthesize(
            text=dialogue.text,
            voice=character.voice,
            emotion=dialogue.emotion,
            rate=character.rate,
            pitch=character.pitch,
            volume=character.volume,
        )

    async def synthesize_one(self, dialogue: Dialogue) -> SynthesisResult:
        try:
            character = self._chars.get(dialogue.character_id)
            if character is None:
                return SynthesisResult(
                    dialogue_id=dialogue.id, success=False,
                    error=f"character not found: {dialogue.character_id}",
                )
            data = await self._tts.synthesize(
                text=dialogue.text,
                voice=character.voice,
                emotion=dialogue.emotion,
                rate=character.rate,
                pitch=character.pitch,
                volume=character.volume,
            )
            base = dialogue.filename or f"{character.name}_{dialogue.id}"
            ext = self._tts.output_extension
            audio_path = self._audio.save(dialogue.id, f"{base}.{ext}", data)
            updated = dialogue.model_copy(update={
                "audio_path": audio_path,
                "synthesized_at": datetime.utcnow(),
            })
            self._dialogs.upsert(updated)
            return SynthesisResult(
                dialogue_id=dialogue.id, success=True, audio_path=audio_path,
            )
        except Exception as e:
            return SynthesisResult(
                dialogue_id=dialogue.id, success=False, error=str(e),
            )

    async def batch(self, dialogues: list[Dialogue]) -> AsyncIterator[SynthesisResult]:
        for d in dialogues:
            yield await self.synthesize_one(d)
