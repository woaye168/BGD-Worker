# @purpose: 角色业务服务（创建/查询/更新/删除）
# @layer: logic
# @contract:
#   - CharacterService.{list, get, create, update, delete, find_by_name}
# @depends:
#   - uuid (stdlib)
#   - ../contract/models.py: Character
#   - ../contract/ports.py: CharacterRepository
#   - ../contract/errors.py: NotFoundError, ValidationError
# @invariants:
#   - id 由 uuid4().hex[:10] 生成，仅在 create 时确定，update 不允许改 id
#   - name 必须非空（trim 后），其余字段使用 Character 模型默认值兜底
#   - update 接受 partial dict，未提供字段保留原值
#   - 修改角色配置不会自动失效已合成的对话音频（见 AI_MAINTENANCE.md 关键不变量）

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from contract.errors import NotFoundError, ValidationError
from contract.models import Character
from contract.ports import CharacterRepository


class CharacterService:
    def __init__(self, repo: CharacterRepository) -> None:
        self._repo = repo

    def list(self) -> list[Character]:
        return self._repo.list()

    def get(self, id: str) -> Character:
        c = self._repo.get(id)
        if c is None:
            raise NotFoundError(f"character not found: {id}")
        return c

    def find_by_name(self, name: str) -> Optional[Character]:
        if not name:
            return None
        target = name.strip()
        for c in self._repo.list():
            if c.name == target:
                return c
        return None

    def create(self, data: dict) -> Character:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValidationError("角色名不能为空")
        payload = {**data, "id": uuid4().hex[:10], "name": name}
        character = Character.model_validate(payload)
        self._repo.upsert(character)
        return character

    def update(self, id: str, patch: dict) -> Character:
        current = self.get(id)
        clean_patch = {k: v for k, v in patch.items() if k != "id"}
        if "name" in clean_patch:
            name = (clean_patch["name"] or "").strip()
            if not name:
                raise ValidationError("角色名不能为空")
            clean_patch["name"] = name
        updated = current.model_copy(update=clean_patch)
        self._repo.upsert(updated)
        return updated

    def delete(self, id: str) -> None:
        if not self._repo.delete(id):
            raise NotFoundError(f"character not found: {id}")
