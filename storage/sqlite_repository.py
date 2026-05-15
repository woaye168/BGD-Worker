# @purpose: SQLite 仓储实现（characters + dialogues 同库），含从旧 JSON 文件的一次性迁移
# @layer: adapter
# @contract:
#   - SQLiteCharacterRepository(db_path, data_dir=None).{list, get, upsert, delete}
#   - SQLiteDialogueRepository(db_path, data_dir=None).{list, get, upsert, delete, bulk_add, reorder}
# @depends:
#   - sqlite3, json, threading, logging, contextlib (stdlib)
#   - ../contract/models.py: Character, Dialogue
#   - ../contract/errors.py: StorageError
# @invariants:
#   - 一个 .db 文件存放 characters + dialogues 两表；首次构造时建表 + 若 data_dir 给定则尝试 JSON 迁移
#   - Dialogue 顺序由 position INTEGER 列显式表达；list() ORDER BY position；
#     upsert 新增时取 MAX(position)+1；bulk_add 连续递增；reorder 重写 position
#   - 旧 JSON 迁移幂等：仅当目标表为空且对应 .json 文件存在时执行；成功后 .json → .json.bak
#   - 迁移过程任何异常都包成 StorageError，原文件不动以便用户排查
#   - 连接采用"每次开新连接 + 上下文管理器自动提交/回滚/关闭"，免线程亲和性问题；
#     sqlite3 open 极快，单机工具量级足够
#   - reorder 集合不匹配抛 StorageError（与 Protocol 不变量一致）

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from contract.errors import StorageError
from contract.models import Character, Dialogue

_logger = logging.getLogger("npc.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
  id              TEXT    PRIMARY KEY,
  name            TEXT    NOT NULL,
  voice           TEXT    NOT NULL,
  rate            REAL    NOT NULL DEFAULT 1.0,
  pitch           REAL    NOT NULL DEFAULT 1.0,
  volume          REAL    NOT NULL DEFAULT 1.0,
  default_emotion TEXT    NOT NULL DEFAULT 'neutral'
);
CREATE TABLE IF NOT EXISTS dialogues (
  id              TEXT    PRIMARY KEY,
  character_id    TEXT    NOT NULL,
  text            TEXT    NOT NULL,
  emotion         TEXT    NOT NULL DEFAULT 'neutral',
  scene           TEXT    NOT NULL DEFAULT '',
  filename        TEXT,
  created_at      TEXT    NOT NULL,
  audio_path      TEXT,
  synthesized_at  TEXT,
  position        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialogues_position ON dialogues(position);
"""

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[Path] = set()


def _ensure_db(db_path: Path, data_dir: Optional[Path]) -> None:
    with _INIT_LOCK:
        if db_path in _INITIALIZED:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_SCHEMA)
            if data_dir is not None:
                _migrate_json_if_needed(conn, data_dir)
            conn.commit()
        finally:
            conn.close()
        _INITIALIZED.add(db_path)


def _migrate_json_if_needed(conn: sqlite3.Connection, data_dir: Path) -> None:
    chars_json = data_dir / "characters.json"
    if conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0 and chars_json.exists():
        try:
            rows = json.loads(chars_json.read_text(encoding="utf-8"))
            for r in rows:
                conn.execute(
                    "INSERT INTO characters (id,name,voice,rate,pitch,volume,default_emotion) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r["id"], r["name"], r["voice"],
                     r.get("rate", 1.0), r.get("pitch", 1.0), r.get("volume", 1.0),
                     r.get("default_emotion", "neutral")),
                )
            chars_json.rename(chars_json.with_suffix(".json.bak"))
            _logger.info("迁移角色 %d 条 → SQLite；characters.json → .bak", len(rows))
        except Exception as e:
            raise StorageError(f"角色 JSON 迁移失败：{e}") from e

    dlg_json = data_dir / "dialogues.json"
    if conn.execute("SELECT COUNT(*) FROM dialogues").fetchone()[0] == 0 and dlg_json.exists():
        try:
            rows = json.loads(dlg_json.read_text(encoding="utf-8"))
            for idx, r in enumerate(rows):
                conn.execute(
                    "INSERT INTO dialogues (id,character_id,text,emotion,scene,filename,"
                    "created_at,audio_path,synthesized_at,position) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["character_id"], r["text"],
                     r.get("emotion", "neutral"), r.get("scene", ""),
                     r.get("filename"), r["created_at"],
                     r.get("audio_path"), r.get("synthesized_at"), idx),
                )
            dlg_json.rename(dlg_json.with_suffix(".json.bak"))
            _logger.info("迁移对话 %d 条 → SQLite；dialogues.json → .bak", len(rows))
        except Exception as e:
            raise StorageError(f"对话 JSON 迁移失败：{e}") from e


@contextmanager
def _open(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class SQLiteCharacterRepository:
    def __init__(self, db_path: Path, data_dir: Optional[Path] = None) -> None:
        self._path = Path(db_path)
        _ensure_db(self._path, data_dir)

    def list(self) -> list[Character]:
        with _open(self._path) as c:
            rows = c.execute("SELECT * FROM characters ORDER BY name").fetchall()
        return [Character.model_validate(dict(r)) for r in rows]

    def get(self, id: str) -> Optional[Character]:
        with _open(self._path) as c:
            r = c.execute("SELECT * FROM characters WHERE id = ?", (id,)).fetchone()
        return Character.model_validate(dict(r)) if r else None

    def upsert(self, character: Character) -> None:
        d = character.model_dump(mode="json")
        with _open(self._path) as c:
            c.execute(
                """INSERT INTO characters (id,name,voice,rate,pitch,volume,default_emotion)
                   VALUES (:id,:name,:voice,:rate,:pitch,:volume,:default_emotion)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, voice=excluded.voice, rate=excluded.rate,
                     pitch=excluded.pitch, volume=excluded.volume,
                     default_emotion=excluded.default_emotion""",
                d,
            )

    def delete(self, id: str) -> bool:
        with _open(self._path) as c:
            r = c.execute("DELETE FROM characters WHERE id = ?", (id,))
            return r.rowcount > 0


_DIALOGUE_INSERT_SQL = (
    "INSERT INTO dialogues (id,character_id,text,emotion,scene,filename,"
    "created_at,audio_path,synthesized_at,position) "
    "VALUES (:id,:character_id,:text,:emotion,:scene,:filename,"
    ":created_at,:audio_path,:synthesized_at,:position)"
)


class SQLiteDialogueRepository:
    def __init__(self, db_path: Path, data_dir: Optional[Path] = None) -> None:
        self._path = Path(db_path)
        _ensure_db(self._path, data_dir)

    @staticmethod
    def _row_to_dialogue(r: sqlite3.Row) -> Dialogue:
        d = dict(r)
        d.pop("position", None)
        return Dialogue.model_validate(d)

    def list(self) -> list[Dialogue]:
        with _open(self._path) as c:
            rows = c.execute("SELECT * FROM dialogues ORDER BY position").fetchall()
        return [self._row_to_dialogue(r) for r in rows]

    def get(self, id: str) -> Optional[Dialogue]:
        with _open(self._path) as c:
            r = c.execute("SELECT * FROM dialogues WHERE id = ?", (id,)).fetchone()
        return self._row_to_dialogue(r) if r else None

    def upsert(self, dialogue: Dialogue) -> None:
        d = dialogue.model_dump(mode="json")
        with _open(self._path) as c:
            exists = c.execute("SELECT 1 FROM dialogues WHERE id = ?", (d["id"],)).fetchone()
            if exists is None:
                d["position"] = (c.execute("SELECT COALESCE(MAX(position), -1) FROM dialogues").fetchone()[0]) + 1
                c.execute(_DIALOGUE_INSERT_SQL, d)
            else:
                c.execute(
                    """UPDATE dialogues SET
                         character_id=:character_id, text=:text, emotion=:emotion, scene=:scene,
                         filename=:filename, created_at=:created_at, audio_path=:audio_path,
                         synthesized_at=:synthesized_at
                       WHERE id = :id""",
                    d,
                )

    def delete(self, id: str) -> bool:
        with _open(self._path) as c:
            r = c.execute("DELETE FROM dialogues WHERE id = ?", (id,))
            return r.rowcount > 0

    def bulk_add(self, dialogues: list[Dialogue]) -> None:
        if not dialogues:
            return
        with _open(self._path) as c:
            start = c.execute("SELECT COALESCE(MAX(position), -1) FROM dialogues").fetchone()[0] + 1
            for offset, d in enumerate(dialogues):
                row = d.model_dump(mode="json")
                row["position"] = start + offset
                c.execute(_DIALOGUE_INSERT_SQL, row)

    def reorder(self, ids: list[str]) -> None:
        with _open(self._path) as c:
            existing = {r[0] for r in c.execute("SELECT id FROM dialogues").fetchall()}
            if set(ids) != existing:
                raise StorageError("reorder ids 必须与现有对话 id 集合完全相同")
            for pos, id in enumerate(ids):
                c.execute("UPDATE dialogues SET position = ? WHERE id = ?", (pos, id))
