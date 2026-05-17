# @purpose: 在线模型 catalog 客户端（GitHub Release JSON + zip 流式下载）+ raw manifest 共享缓存
# @layer: adapter
# @contract:
#   - GithubReleaseCatalog(url, models_dir, cache_ttl_sec).{fetch, fetch_raw, download}
# @depends:
#   - asyncio, json, time, logging, zipfile, shutil (stdlib)
#   - pathlib
#   - ../contract/models.py: TTSModel
#   - ../contract/errors.py: ModelError
#   - ./_download.py: stream_download, sha256_file
# @invariants:
#   - catalog JSON schema v2 形状（每段独立 version）:
#       {"schema_version": 2,
#        "windows_x64": {"version": str, "download_url", "size_bytes", "sha256"},
#        "windows_x64_cpu": {...},
#        "windows_x64_amd_rocm": {...},
#        "windows_x64_nvidia_cuda": {...},
#        "models": [TTSModel-dict, ...]}
#     v1 兼容：顶层 "version" 字段存在时，作为段缺 version 的兜底（runtime_installer 处理）
#     每条 model 必含 {id, name, download_url}；engine 缺省 "local"；source 强制改为 "catalog"
#   - fetch() 缓存 cache_ttl_sec 秒；force=True 强制重新拉取（带 CDN 缓存破坏 query string）
#   - fetch_raw() 返回原始 manifest dict 供 runtime_installer 消费（共享缓存层）
#   - fetch() 失败抛 ModelError（含底层原因）
#   - download() 是 async generator，产出事件:
#       {"phase":"start"|"downloading"|"verifying"|"extracting"|"done", ...}
#       downloading 事件含 received/total/percent；其余事件含 model_id
#   - download() 下载到 models_dir/.<id>.zip.tmp，校验/解压后落到 models_dir/<id>/，
#     最后写入 meta.json（catalog 元数据），删除临时 zip
#   - 目标目录已存在 → 抛 ModelError（防止覆盖已有模型）
#   - sha256 字段非空时强制校验；不匹配则清理临时文件并抛 ModelError
#   - zip 解压前防 path traversal：每个 zip 内 entry 必须落在 target 子目录下

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional

from contract.errors import ModelError
from contract.models import TTSModel

from ._download import URLError, sha256_file, stream_download

logger = logging.getLogger(__name__)


class GithubReleaseCatalog:
    def __init__(self, url: str, models_dir: Path, cache_ttl_sec: int = 3600):
        self._url = url
        self._models_dir = Path(models_dir)
        self._ttl = max(0, int(cache_ttl_sec))
        # 缓存 (timestamp, raw_payload_dict)；models 按需从 raw 派生
        self._raw_cache: Optional[tuple[float, dict]] = None

    async def fetch_raw(self, force: bool = False) -> dict:
        """返回原始 manifest dict（含 windows_x64* 段 + models 段）。

        force=True 时除清本地缓存外，还附加 query string 破坏 GitHub raw / CDN 边缘缓存
        （raw.githubusercontent.com 默认 5 分钟 TTL，HF resolve 类似）。
        runtime_installer / catalog 共用此方法读取同源 JSON。
        """
        if not self._url:
            raise ModelError("未配置 catalog URL，请先在「软件设置」填入")
        now = time.time()
        if not force and self._raw_cache and (now - self._raw_cache[0]) < self._ttl:
            return self._raw_cache[1]
        try:
            payload = await asyncio.to_thread(self._fetch_sync, force)
        except URLError as e:
            raise ModelError(f"catalog 拉取失败(网络): {e}") from e
        except Exception as e:
            raise ModelError(f"catalog 拉取失败: {e}") from e
        self._raw_cache = (now, payload)
        return payload

    async def fetch(self, force: bool = False) -> list[TTSModel]:
        payload = await self.fetch_raw(force=force)
        models = self._parse(payload)
        logger.info("catalog fetched: url=%s models=%d force=%s", self._url, len(models), force)
        return models

    async def download(self, id: str) -> AsyncIterator[dict]:
        models = await self.fetch()
        match = next((m for m in models if m.id == id), None)
        if match is None:
            raise ModelError(f"catalog 中未找到模型: {id}")
        if not match.download_url:
            raise ModelError(f"模型缺 download_url: {id}")
        target_dir = self._models_dir / match.id
        if target_dir.exists():
            raise ModelError(f"模型目录已存在(请先删除再重装): {target_dir}")

        self._models_dir.mkdir(parents=True, exist_ok=True)
        tmp_zip = self._models_dir / f".{match.id}.zip.tmp"
        if tmp_zip.exists():
            tmp_zip.unlink()

        yield {"phase": "start", "model_id": match.id, "message": f"开始下载 {match.name or match.id}"}

        try:
            async for evt in stream_download(match.download_url, tmp_zip, total_hint=match.size_bytes or None):
                evt["model_id"] = match.id
                yield evt
        except Exception as e:
            tmp_zip.unlink(missing_ok=True)
            raise ModelError(f"下载失败: {e}") from e

        if match.sha256:
            yield {"phase": "verifying", "model_id": match.id, "message": "校验 sha256"}
            actual = await asyncio.to_thread(sha256_file, tmp_zip)
            if actual.lower() != match.sha256.lower():
                tmp_zip.unlink(missing_ok=True)
                raise ModelError(
                    f"sha256 不匹配: 期望 {match.sha256}, 实际 {actual}"
                )

        yield {"phase": "extracting", "model_id": match.id, "message": "解压"}
        try:
            await asyncio.to_thread(self._extract_zip, tmp_zip, target_dir)
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            tmp_zip.unlink(missing_ok=True)
            raise ModelError(f"解压失败: {e}") from e

        # 写入 catalog 元数据为 meta.json（覆盖 zip 内可能的 meta.json）
        meta_payload = match.model_dump(mode="json")
        meta_payload["installed"] = True
        meta_payload["source"] = "catalog"
        try:
            (target_dir / "meta.json").write_text(
                json.dumps(meta_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise ModelError(f"写入 meta.json 失败: {e}") from e

        tmp_zip.unlink(missing_ok=True)
        logger.info("model downloaded: id=%s size=%s", match.id, match.size_bytes)
        yield {"phase": "done", "model_id": match.id, "message": "安装完成"}

    # internal

    def _fetch_sync(self, force: bool = False) -> dict:
        from urllib.request import Request, urlopen

        url = self._url
        headers = {"User-Agent": "npc-voice-gen/0.1", "Accept": "application/json"}
        if force:
            # 附加时间戳 query 破坏 GitHub raw / HF resolve / 各级 CDN 边缘缓存
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}_={int(time.time())}"
            headers["Cache-Control"] = "no-cache"
            headers["Pragma"] = "no-cache"
        req = Request(url, headers=headers)
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("catalog JSON 顶层必须为 object")
        return data

    def _parse(self, payload: dict) -> list[TTSModel]:
        items = payload.get("models")
        if not isinstance(items, list):
            raise ModelError("catalog JSON 缺 models 数组")
        out: list[TTSModel] = []
        for raw in items:
            if not isinstance(raw, dict):
                logger.warning("跳过非 dict 模型条目: %r", raw)
                continue
            data = dict(raw)
            data.setdefault("engine", "local")
            data["source"] = "catalog"
            data["installed"] = False
            try:
                out.append(TTSModel.model_validate(data))
            except Exception as e:
                logger.warning("跳过损坏 catalog 条目 id=%s: %s", data.get("id"), e)
        return out

    def _extract_zip(self, zip_path: Path, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            # path traversal 防护
            for name in zf.namelist():
                # zip 内常见的根目录前缀去除：直接落 target_dir
                if name.endswith("/"):
                    continue
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ModelError(f"zip 含非法路径: {name}")
            zf.extractall(target_dir)
