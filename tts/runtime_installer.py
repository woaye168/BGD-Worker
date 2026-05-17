# @purpose: 本地 TTS 运行时（GPT-SoVITS 等）按需安装/卸载，支持多硬件 target 变体
# @layer: adapter
# @contract:
#   - LocalTTSRuntimeInstaller(install_dir, manifest_url, target).{name, is_installed,
#       installed_version, installed_target, install, uninstall}
# @depends:
#   - asyncio, json, shutil, sys, logging, zipfile (stdlib)
#   - pathlib
#   - ../contract/errors.py: ModelError
#   - ./_download.py: stream_download, sha256_file
# @invariants:
#   - 当前仅支持 Windows（用户决策 Q2）；非 win32 调 install() 抛 ModelError，
#     message 含 "暂不支持" + 平台名，供前端展示
#   - manifest_url 指向独立的运行时清单 JSON（与模型 catalog 是两套），形状:
#       {
#         "version": str,
#         "windows_x64_cpu": {"download_url", "size_bytes", "sha256"},
#         "windows_x64_amd_rocm": {...},        # 可选；缺则该 target 不可装
#         "windows_x64_nvidia_cuda": {...},     # 可选；同上
#         "windows_x64": {...}                  # 旧字段，作 cpu 别名兜底（兼容 v0.2.x 旧 catalog）
#       }
#     按 target 选段：优先 windows_x64_<target_normalized>（'-' → '_'），
#     若该 target 段缺失且 target == 'cpu' 则 fallback `windows_x64`
#   - VERSION 文件格式 `<ver> <target>`（如 "0.3.0 amd-rocm"）；
#     旧 runtime 包仅 `<ver>` → 解析时 target 默认 'cpu'
#   - is_installed = install_dir/VERSION 文件存在；
#     installed_version() / installed_target() 解析返回各部分
#   - 切 target 流程：调用方（api.deps）发现 installed_target ≠ cfg.local.target 时，
#     在 invalidate_caches 前先 uninstall() 旧版，再用新 target 构造 installer
#   - install() 是 async generator，产出事件:
#       {"phase":"start"|"downloading"|"verifying"|"extracting"|"done"|"error", ...,
#        "target": str}  # 新加 target 字段供前端区分
#     成功结束后 install_dir 内含 VERSION 文件标记 `<ver> <target>`
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
_VALID_TARGETS = ("cpu", "amd-rocm", "nvidia-cuda")


def _parse_version_file(text: str) -> tuple[Optional[str], str]:
    """解析 VERSION 文件内容；返回 (version, target)。

    新格式："0.3.0 amd-rocm" → ("0.3.0", "amd-rocm")
    旧格式："0.2.9" → ("0.2.9", "cpu")  # 兼容 v0.2.x runtime 包
    空/损坏 → (None, "cpu")
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, "cpu"
    parts = stripped.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], "cpu"
    return parts[0], parts[1].strip() or "cpu"


def _manifest_slot_key(target: str) -> str:
    """把 target 转成 catalog/manifest 里的字段名。

    'amd-rocm' → 'windows_x64_amd_rocm'（连字符变下划线，JSON 字段名约定）
    """
    return f"windows_x64_{target.replace('-', '_')}"


class LocalTTSRuntimeInstaller:
    def __init__(self, install_dir: Path, manifest_url: str = "", target: str = "cpu"):
        self._install_dir = Path(install_dir)
        self._manifest_url = manifest_url
        if target not in _VALID_TARGETS:
            raise ValueError(f"invalid target: {target}; valid: {_VALID_TARGETS}")
        self._target = target

    @property
    def name(self) -> str:
        return "local-tts"

    @property
    def target(self) -> str:
        """本 installer 实例用于安装哪个 target 的运行时（构造时确定）。"""
        return self._target

    def is_installed(self) -> bool:
        return (self._install_dir / _VERSION_FILE).exists()

    def installed_version(self) -> Optional[str]:
        """从 VERSION 文件解析版本号；解析失败返回 None。"""
        f = self._install_dir / _VERSION_FILE
        if not f.exists():
            return None
        try:
            ver, _ = _parse_version_file(f.read_text(encoding="utf-8"))
            return ver
        except Exception:
            return None

    def installed_target(self) -> Optional[str]:
        """从 VERSION 文件解析 target；未安装返回 None。

        旧 runtime 包 VERSION 仅含版本号 → target 视为 'cpu'。
        """
        f = self._install_dir / _VERSION_FILE
        if not f.exists():
            return None
        try:
            _, target = _parse_version_file(f.read_text(encoding="utf-8"))
            return target
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

        yield {"phase": "start", "message": f"获取运行时清单 (target={self._target})", "target": self._target}

        try:
            manifest = await asyncio.to_thread(self._fetch_manifest_sync)
        except URLError as e:
            raise ModelError(f"manifest 拉取失败(网络): {e}") from e
        except Exception as e:
            raise ModelError(f"manifest 拉取失败: {e}") from e

        # 按 target 选段：优先 windows_x64_<target_normalized>，
        # cpu target 若该段缺失则 fallback 旧 `windows_x64`（兼容 v0.2.x catalog）
        slot_key = _manifest_slot_key(self._target)
        slot = manifest.get(slot_key)
        if (not isinstance(slot, dict) or not slot.get("download_url")) and self._target == "cpu":
            slot = manifest.get("windows_x64")  # legacy fallback
            slot_key = "windows_x64 (legacy)"
        if not isinstance(slot, dict) or not slot.get("download_url"):
            raise ModelError(
                f"manifest 缺 target={self._target} 对应段（{_manifest_slot_key(self._target)}）。"
                f"该后端可能尚未发布；请切到其他后端或等待 catalog 更新。"
            )
        version = str(manifest.get("version") or "")
        download_url = slot["download_url"]
        size_bytes = int(slot.get("size_bytes") or 0)
        expected_sha = (slot.get("sha256") or "").lower()
        logger.info(
            "install runtime: target=%s slot=%s url=%s size=%dB",
            self._target, slot_key, download_url, size_bytes,
        )

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

            yield {"phase": "extracting", "message": "解压运行时", "target": self._target}
            # 清空目标后重建（前面已校验未安装）
            if self._install_dir.exists():
                shutil.rmtree(self._install_dir)
            self._install_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._extract_zip, tmp_zip, self._install_dir)

            # 写 VERSION 标记 `<ver> <target>`；installed_target() 据此判断切变体需重装
            # 注：zip 内层的 VERSION 文件由 build_runtime.py 已写入相同格式，这里覆盖确保一致
            (self._install_dir / _VERSION_FILE).write_text(
                f"{version} {self._target}", encoding="utf-8"
            )
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

        logger.info(
            "local-tts runtime installed: version=%s target=%s dir=%s",
            version, self._target, self._install_dir,
        )
        yield {"phase": "done", "message": f"已安装 {version} ({self._target})", "target": self._target}

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
