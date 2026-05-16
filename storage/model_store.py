# @purpose: 文件系统模型存储（扫描 data_dir/models/<id>/，每个目录含 meta.json）
# @layer: adapter
# @contract:
#   - FileSystemModelStore(root).{list_installed, get, import_from_path, remove, root}
# @depends:
#   - json, shutil, logging (stdlib)
#   - pathlib
#   - ../contract/models.py: TTSModel
#   - ../contract/errors.py: ModelError, ValidationError, NotFoundError
# @invariants:
#   - 模型目录名 = 模型 id（即使 meta.json 内 id 不同，以目录名为权威）
#   - meta.json 必须含字段 {id, engine, name}；缺失则跳过（logger.warning 记录）
#   - import_from_path 仅接受目录入参（zip 解压在 tts/catalog_client 内做）；
#     目录必须含合法 meta.json；目标 id 已存在则抛 ModelError
#   - list_installed 是只读扫描，不修改任何文件；files/size_bytes 字段每次扫描重算
#   - remove 删除整个模型目录；不存在返回 False，存在但删除失败抛 ModelError

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from contract.errors import ModelError, ValidationError
from contract.models import TTSModel

logger = logging.getLogger(__name__)

_META_FILE = "meta.json"


class FileSystemModelStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def root(self) -> Path:
        return self._root

    def list_installed(self) -> list[TTSModel]:
        if not self._root.exists():
            return []
        result: list[TTSModel] = []
        for entry in sorted(self._root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            model = self._load_meta(entry)
            if model is not None:
                result.append(model)
        return result

    def get(self, id: str) -> Optional[TTSModel]:
        if not id:
            return None
        target = self._root / id
        if not target.is_dir():
            return None
        return self._load_meta(target)

    def import_from_path(self, path: Path) -> TTSModel:
        src = Path(path)
        if not src.is_dir():
            raise ValidationError(f"导入路径必须是目录: {src}")
        meta_file = src / _META_FILE
        if not meta_file.exists():
            raise ValidationError(f"目录缺 {_META_FILE}: {src}")
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValidationError(f"{_META_FILE} 解析失败: {e}") from e
        model_id = (data.get("id") or "").strip()
        if not model_id:
            raise ValidationError("meta.json 缺 id 字段")
        target = self._root / model_id
        if target.exists():
            raise ModelError(f"模型 id 已存在: {model_id}")
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(src, target)
        except Exception as e:
            raise ModelError(f"模型目录拷贝失败: {e}") from e
        # 标记 source=imported（如果用户没有显式标记）
        if not data.get("source"):
            data["source"] = "imported"
            (target / _META_FILE).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        loaded = self._load_meta(target)
        if loaded is None:
            raise ModelError(f"导入后无法加载模型: {model_id}")
        logger.info("imported model: id=%s from=%s", model_id, src)
        return loaded

    def remove(self, id: str) -> bool:
        if not id:
            return False
        target = self._root / id
        if not target.exists():
            return False
        try:
            shutil.rmtree(target)
        except Exception as e:
            raise ModelError(f"模型目录删除失败 {id}: {e}") from e
        logger.info("removed model: id=%s", id)
        return True

    # internal

    def _load_meta(self, model_dir: Path) -> Optional[TTSModel]:
        meta_file = model_dir / _META_FILE
        if not meta_file.exists():
            logger.warning("跳过模型目录(缺 %s): %s", _META_FILE, model_dir)
            return None
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("跳过模型目录(meta.json 解析失败): %s: %s", model_dir, e)
            return None
        # 目录名权威
        data["id"] = model_dir.name
        data["installed"] = True
        # 扫描真实文件
        files: list[str] = []
        size_bytes = 0
        for p in model_dir.rglob("*"):
            if p.is_file() and p.name != _META_FILE:
                files.append(str(p.relative_to(model_dir)).replace("\\", "/"))
                try:
                    size_bytes += p.stat().st_size
                except OSError:
                    pass
        data["files"] = files
        data["size_bytes"] = size_bytes
        try:
            return TTSModel.model_validate(data)
        except Exception as e:
            logger.warning("跳过模型目录(字段校验失败): %s: %s", model_dir, e)
            return None
