# @purpose: 角色/对话的 JSON 文件仓储（同步、整文件读写）
# @layer: adapter
# @contract:
#   - JSONCharacterRepository(path).{list, get, upsert, delete}
#   - JSONDialogueRepository(path).{list, get, upsert, delete, bulk_add, reorder}
# @depends:
#   - json, threading, pathlib (stdlib)
#   - ../contract/models.py: Character, Dialogue
#   - ../contract/errors.py: StorageError
# @invariants:
#   - 写入采用 write-temp + replace 原子模式，崩溃不会半写
#   - 同一进程内使用 RLock 串行化读写；跨进程并发不安全（单机工具场景可接受）
#   - 文件不存在则初始化为空数组 "[]"；datetime 通过 default=str 序列化为 ISO 字符串
#   - JSON 文件结构是 list[dict]，每条记录用 model_dump(mode='json') 序列化
#   - reorder 要求 ids 集合与现有对话 id 集合完全相同，否则抛 StorageError（防止误传丢数据）

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from contract.errors import StorageError
from contract.models import Character, Dialogue


class _JSONFileBase:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

    def _read(self) -> list[dict]:
        with self._lock:
            text = self._path.read_text(encoding="utf-8")
            return json.loads(text) if text.strip() else []

    def _write(self, rows: list[dict]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(self._path)


class JSONCharacterRepository(_JSONFileBase):
    def list(self) -> list[Character]:
        return [Character.model_validate(r) for r in self._read()]

    def get(self, id: str) -> Optional[Character]:
        for r in self._read():
            if r.get("id") == id:
                return Character.model_validate(r)
        return None

    def upsert(self, character: Character) -> None:
        with self._lock:
            rows = self._read()
            payload = character.model_dump(mode="json")
            for i, r in enumerate(rows):
                if r.get("id") == character.id:
                    rows[i] = payload
                    break
            else:
                rows.append(payload)
            self._write(rows)

    def delete(self, id: str) -> bool:
        with self._lock:
            rows = self._read()
            new_rows = [r for r in rows if r.get("id") != id]
            if len(new_rows) == len(rows):
                return False
            self._write(new_rows)
            return True


class JSONDialogueRepository(_JSONFileBase):
    def list(self) -> list[Dialogue]:
        return [Dialogue.model_validate(r) for r in self._read()]

    def get(self, id: str) -> Optional[Dialogue]:
        for r in self._read():
            if r.get("id") == id:
                return Dialogue.model_validate(r)
        return None

    def upsert(self, dialogue: Dialogue) -> None:
        with self._lock:
            rows = self._read()
            payload = dialogue.model_dump(mode="json")
            for i, r in enumerate(rows):
                if r.get("id") == dialogue.id:
                    rows[i] = payload
                    break
            else:
                rows.append(payload)
            self._write(rows)

    def delete(self, id: str) -> bool:
        with self._lock:
            rows = self._read()
            new_rows = [r for r in rows if r.get("id") != id]
            if len(new_rows) == len(rows):
                return False
            self._write(new_rows)
            return True

    def bulk_add(self, dialogues: list[Dialogue]) -> None:
        with self._lock:
            rows = self._read()
            rows.extend(d.model_dump(mode="json") for d in dialogues)
            self._write(rows)

    def reorder(self, ids: list[str]) -> None:
        with self._lock:
            rows = self._read()
            by_id = {r.get("id"): r for r in rows}
            if set(ids) != set(by_id.keys()):
                raise StorageError("reorder ids 必须与现有对话 id 集合完全相同")
            self._write([by_id[i] for i in ids])
