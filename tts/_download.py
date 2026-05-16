# @purpose: 流式 HTTP 下载工具（stdlib only，避免引入 runtime 依赖）
# @layer: adapter
# @contract:
#   - stream_download(url, target, total_hint, chunk_size) -> AsyncIterator[dict]
#       yields {"phase":"downloading","percent":float,"received":int,"total":int?}
#   - sha256_file(path) -> str
# @depends:
#   - asyncio, hashlib, urllib.request, urllib.error (stdlib)
# @invariants:
#   - 同步 urllib 操作通过 asyncio.to_thread 异步化，避免阻塞事件循环
#   - chunk_size 默认 64KB；下载到 target.with_suffix('.partial')，成功后 rename
#   - percent 仅在 total>0 时给出（HTTP Content-Length 缺失时只给 received 字节数）
#   - 异常一律抛 IOError/OSError，调用方负责转 ModelError

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


def _open_url(url: str, timeout: float = 30.0):
    req = Request(url, headers={"User-Agent": "npc-voice-gen/0.1"})
    return urlopen(req, timeout=timeout)


async def stream_download(
    url: str,
    target: Path,
    total_hint: Optional[int] = None,
    chunk_size: int = 64 * 1024,
) -> AsyncIterator[dict]:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")

    def _open():
        resp = _open_url(url, timeout=30.0)
        total = int(resp.headers.get("Content-Length") or 0) or (total_hint or 0)
        return resp, total

    resp, total = await asyncio.to_thread(_open)
    try:
        received = 0
        with partial.open("wb") as f:
            while True:
                chunk = await asyncio.to_thread(resp.read, chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                evt = {"phase": "downloading", "received": received}
                if total > 0:
                    evt["total"] = total
                    evt["percent"] = round(received / total * 100, 2)
                yield evt
    finally:
        await asyncio.to_thread(resp.close)

    await asyncio.to_thread(partial.replace, target)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


__all__ = ["stream_download", "sha256_file", "URLError"]
