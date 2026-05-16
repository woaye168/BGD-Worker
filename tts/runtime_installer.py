# @purpose: 本地 TTS 运行时（GPT-SoVITS 等）按需安装/卸载
# @layer: adapter
# @contract:
#   - LocalTTSRuntimeInstaller(install_dir, manifest_url).{name, is_installed,
#       installed_version, install, uninstall}
# @depends:
#   - asyncio, json, shutil, sys, logging, zipfile (stdlib)
#   - pathlib
#   - ../contract/errors.py: ModelError
#   - ./_download.py: stream_download, sha256_file
# @invariants:
#   - 当前仅支持 Windows（用户决策 Q2）；非 win32 调 install() 抛 ModelError，
#     message 含 "暂不支持" + 平台名，供前端展示
#   - manifest_url 指向独立的运行时清单 JSON（与模型 catalog 是两套），形状:
#       {"version": str, "windows_x64": {"download_url": str, "size_bytes": int, "sha256": str}}
#     是否提供其他平台 key 由独立交付的 manifest 决定（当前忽略）
#   - is_installed = install_dir/VERSION 文件存在；installed_version = 该文件内容
#   - install() 是 async generator，产出事件:
#       {"phase":"start"|"downloading"|"verifying"|"extracting"|"done"|"error", ...}
#     成功结束后 install_dir 内含 VERSION 文件标记版本
#   - 安装失败会清空 install_dir，避免半成品状态
#   - uninstall() 不删 manifest 缓存，仅删运行时目录；非空目录 rmtree

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional

from contract.errors import ModelError

from ._download import URLError, sha256_file, stream_download

logger = logging.getLogger(__name__)

_VERSION_FILE = "VERSION"


class LocalTTSRuntimeInstaller:
    def __init__(self, install_dir: Path, manifest_url: str = ""):
        self._install_dir = Path(install_dir)
        self._manifest_url = manifest_url

    @property
    def name(self) -> str:
        return "local-tts"

    def is_installed(self) -> bool:
        return (self._install_dir / _VERSION_FILE).exists()

    def installed_version(self) -> Optional[str]:
        f = self._install_dir / _VERSION_FILE
        if not f.exists():
            return None
        try:
            return f.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None

    async def install(self) -> AsyncIterator[dict]:
        # 平台门禁
        if sys.platform != "win32":
            raise ModelError(
                f"本地 TTS 运行时当前仅支持 Windows，检测到平台 {sys.platform}（暂不支持）"
            )
        if not self._manifest_url:
            raise ModelError("未配置运行时 manifest URL，请先在「软件设置」填入")
        if self.is_installed():
            raise ModelError(
                f"运行时已安装（版本 {self.installed_version()}），如需重装请先卸载"
            )

        yield {"phase": "start", "message": "获取运行时清单"}

        try:
            manifest = await asyncio.to_thread(self._fetch_manifest_sync)
        except URLError as e:
            raise ModelError(f"manifest 拉取失败(网络): {e}") from e
        except Exception as e:
            raise ModelError(f"manifest 拉取失败: {e}") from e

        slot = manifest.get("windows_x64")
        if not isinstance(slot, dict) or not slot.get("download_url"):
            raise ModelError("manifest 缺 windows_x64.download_url")
        version = str(manifest.get("version") or "")
        download_url = slot["download_url"]
        size_bytes = int(slot.get("size_bytes") or 0)
        expected_sha = (slot.get("sha256") or "").lower()

        self._install_dir.parent.mkdir(parents=True, exist_ok=True)
        # 在父目录下落临时 zip（不在 install_dir 内部，避免清理冲突）
        tmp_zip = self._install_dir.parent / f".{self._install_dir.name}.zip.tmp"
        if tmp_zip.exists():
            tmp_zip.unlink()

        try:
            async for evt in stream_download(download_url, tmp_zip, total_hint=size_bytes or None):
                yield evt

            if expected_sha:
                yield {"phase": "verifying", "message": "校验 sha256"}
                actual = await asyncio.to_thread(sha256_file, tmp_zip)
                if actual.lower() != expected_sha:
                    raise ModelError(
                        f"sha256 不匹配: 期望 {expected_sha}, 实际 {actual}"
                    )

            yield {"phase": "extracting", "message": "解压运行时"}
            # 清空目标后重建（前面已校验未安装）
            if self._install_dir.exists():
                shutil.rmtree(self._install_dir)
            self._install_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._extract_zip, tmp_zip, self._install_dir)

            # 写 VERSION 标记
            (self._install_dir / _VERSION_FILE).write_text(version, encoding="utf-8")
        except ModelError:
            shutil.rmtree(self._install_dir, ignore_errors=True)
            tmp_zip.unlink(missing_ok=True)
            raise
        except Exception as e:
            shutil.rmtree(self._install_dir, ignore_errors=True)
            tmp_zip.unlink(missing_ok=True)
            raise ModelError(f"运行时安装失败: {e}") from e
        finally:
            tmp_zip.unlink(missing_ok=True)

        logger.info("local-tts runtime installed: version=%s dir=%s", version, self._install_dir)
        yield {"phase": "done", "message": f"已安装 {version}"}

    def uninstall(self) -> bool:
        if not self._install_dir.exists():
            return False
        try:
            shutil.rmtree(self._install_dir)
        except Exception as e:
            raise ModelError(f"运行时卸载失败: {e}") from e
        logger.info("local-tts runtime uninstalled: dir=%s", self._install_dir)
        return True

    # internal

    def _fetch_manifest_sync(self) -> dict:
        from urllib.request import Request, urlopen

        req = Request(
            self._manifest_url,
            headers={"User-Agent": "npc-voice-gen/0.1", "Accept": "application/json"},
        )
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("manifest JSON 顶层必须为 object")
        return data

    def _extract_zip(self, zip_path: Path, target_dir: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ModelError(f"zip 含非法路径: {name}")
            zf.extractall(target_dir)
