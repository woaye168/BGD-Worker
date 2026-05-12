# @purpose: 文件系统音频存储（OGG 文件落盘 / 读取 / 删除 / 路径解析）
# @layer: adapter
# @contract:
#   - FileSystemAudioStore(base_dir).{save, open, exists, delete, absolute}
# @depends:
#   - pathlib (stdlib)
#   - ../contract/errors.py: StorageError
# @invariants:
#   - save 返回的相对路径形如 "audio/<id>__<safe_filename>"，可直接存入 Dialogue.audio_path
#   - 文件名经过 _sanitize 仅保留字母数字与 ._- ，其余替换为 _
#   - 文件物理位置 = base_dir / "<id>__<safe_filename>"，base_dir 名称即相对路径首段
#   - 通过 dialogue_id 前缀避免不同对话的同名文件冲突

from pathlib import Path

from contract.errors import StorageError


def _sanitize(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


class FileSystemAudioStore:
    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._segment = self._base.name

    def save(self, dialogue_id: str, filename: str, data: bytes) -> str:
        safe = _sanitize(filename)
        target = self._base / f"{dialogue_id}__{safe}"
        target.write_bytes(data)
        return f"{self._segment}/{target.name}"

    def open(self, audio_path: str) -> bytes:
        p = self._resolve(audio_path)
        if not p.exists():
            raise StorageError(f"audio not found: {audio_path}")
        return p.read_bytes()

    def exists(self, audio_path: str) -> bool:
        return self._resolve(audio_path).exists()

    def delete(self, audio_path: str) -> bool:
        p = self._resolve(audio_path)
        if p.exists():
            p.unlink()
            return True
        return False

    def absolute(self, audio_path: str) -> Path:
        return self._resolve(audio_path)

    def _resolve(self, audio_path: str) -> Path:
        tail = audio_path.split("/", 1)[1] if "/" in audio_path else audio_path
        return self._base / tail
