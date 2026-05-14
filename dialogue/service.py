# @purpose: 对话业务服务（CRUD、合成结果回写、合成失效）
# @layer: logic
# @contract:
#   - DialogueService.{list, get, create, update, delete, bulk_add, mark_synthesized}
# @depends:
#   - uuid, datetime (stdlib)
#   - ../contract/models.py: Dialogue
#   - ../contract/ports.py: DialogueRepository
#   - ../contract/errors.py: NotFoundError, ValidationError
# @invariants:
#   - 修改 text/emotion/character_id 任一字段 → 必须清空 audio_path 与 synthesized_at（合成失效）
#   - 修改 filename 不触发失效（仅影响下次导出文件名）
#   - mark_synthesized 由 synthesis 编排器在写入音频后调用，唯一允许设置 audio_path 的入口

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from contract.errors import NotFoundError, ValidationError
from contract.models import Dialogue
from contract.ports import DialogueRepository

_INVALIDATING_FIELDS = {"text", "emotion", "character_id"}


class DialogueService:
    def __init__(self, repo: DialogueRepository) -> None:
        self._repo = repo

    def list(self) -> list[Dialogue]:
        return self._repo.list()

    def get(self, id: str) -> Dialogue:
        d = self._repo.get(id)
        if d is None:
            raise NotFoundError(f"dialogue not found: {id}")
        return d

    def create(self, data: dict) -> Dialogue:
        text = (data.get("text") or "").strip()
        if not text:
            raise ValidationError("对话内容不能为空")
        if not data.get("character_id"):
            raise ValidationError("必须指定 character_id")
        payload = {
            **data,
            "id": uuid4().hex[:10],
            "text": text,
            "created_at": datetime.utcnow(),
            "audio_path": None,
            "synthesized_at": None,
        }
        dialogue = Dialogue.model_validate(payload)
        self._repo.upsert(dialogue)
        return dialogue

    def update(self, id: str, patch: dict) -> Dialogue:
        current = self.get(id)
        clean = {k: v for k, v in patch.items() if k not in {"id", "created_at"}}
        if "text" in clean:
            clean["text"] = (clean["text"] or "").strip()
            if not clean["text"]:
                raise ValidationError("对话内容不能为空")
        if _INVALIDATING_FIELDS & set(clean.keys()):
            clean["audio_path"] = None
            clean["synthesized_at"] = None
        updated = current.model_copy(update=clean)
        self._repo.upsert(updated)
        return updated

    def delete(self, id: str) -> None:
        if not self._repo.delete(id):
            raise NotFoundError(f"dialogue not found: {id}")

    def bulk_add(self, dialogues: list[Dialogue]) -> int:
        self._repo.bulk_add(dialogues)
        return len(dialogues)

    def mark_synthesized(self, id: str, audio_path: str) -> Dialogue:
        current = self.get(id)
        updated = current.model_copy(update={
            "audio_path": audio_path,
            "synthesized_at": datetime.utcnow(),
        })
        self._repo.upsert(updated)
        return updated
