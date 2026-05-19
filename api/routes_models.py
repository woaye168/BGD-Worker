# @purpose: 模型管理 HTTP 路由（已安装/在线 catalog/SSE 下载/zip 导入/运行时安装卸载）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/models
#   - GET    /api/models/installed              已安装模型列表
#   - GET    /api/models/catalog?force=         在线 catalog（缓存 ttl 内不重拉）
#   - POST   /api/models/catalog/refresh        强制刷新 catalog 缓存（破坏 CDN 边缘缓存）
#   - DELETE /api/models/{id}                   删除已安装模型
#   - POST   /api/models/import                 multipart zip 上传 → 解压 → 导入
#   - POST   /api/models/download/{id}          SSE 流式下载（在 catalog 中找到 id）
#   - GET    /api/models/runtime/status         本地 TTS 运行时所有 target 安装状态
#   - POST   /api/models/runtime/install        SSE 流式安装运行时（仅 win32，body 显式 target）
#   - POST   /api/models/runtime/uninstall      删除指定 target 的运行时
# @depends:
#   - json, logging, tempfile, zipfile (stdlib)
#   - pathlib
#   - fastapi (APIRouter, Depends, File, UploadFile, HTTPException)
#   - fastapi.responses.StreamingResponse
#   - ../contract/models.py: TTSModel
#   - ../contract/ports.py: ModelStore, ModelCatalog, RuntimeInstaller
#   - ../contract/errors.py: ModelError, NotFoundError, ValidationError
#   - ./deps.py
# @invariants:
#   - SSE 事件格式: "data: <json>\n\n"; phase ∈ {start, downloading, verifying,
#     extracting, done, error}; error 事件以正常 200 流响应内联返回（不抛 HTTP 5xx）
#   - 任何修改持久化状态的端点（download/import/delete/install/uninstall）
#     成功后必须调 invalidate_caches()，让 dispatch + voice 列表与状态同步
#   - import 仅接收 .zip 文件；zip 内必须含且仅含一个 meta.json，其所在目录视为模型根
#   - import 走 path traversal 防护（解压前扫描 namelist）
#   - runtime/status 返回所有 target 状态数组，含 installed/version/latest_version/can_update/can_install
#   - runtime/install 接收 body 中的 target 参数，不再因 target 不匹配自动 uninstall 旧变体
#   - 多 target 并存：install 到独立目录，切换 target 只改活跃指向，不删任何已装

from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from contract.errors import ModelError, NotFoundError, ValidationError
from contract.models import TTSModel
from contract.ports import ModelCatalog, ModelStore, RuntimeInstaller

from .deps import (
    _make_runtime_installer,
    get_catalog,
    get_config,
    get_model_store,
    get_runtime_installer,
    invalidate_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])

_VALID_TARGETS = ("cpu", "amd-rocm", "nvidia-cuda")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ============== 已安装模型 ==============


@router.get("/installed", response_model=list[TTSModel])
def list_installed(store: ModelStore = Depends(get_model_store)):
    return store.list_installed()


@router.get("/installed/{id}/audition")
def audition_installed(id: str, store: ModelStore = Depends(get_model_store)):
    """直接返回模型 meta.json 配置的参考音频文件（用于"试听这个 voice 长什么样"）。

    不走 TTS 引擎合成（避免拖累用户、不污染 voice cache）；直接流式吐 ref_audio 原始文件。
    扩展名按文件后缀映射 media type（wav/mp3/ogg/flac/m4a/opus 等）。
    """
    model = store.get(id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"模型未安装: {id}")
    model_dir = store.root() / id
    meta_file = model_dir / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail=f"模型 meta.json 不存在: {meta_file}")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"meta.json 解析失败: {e}") from e
    ref_name = meta.get("ref_audio") or "ref.wav"
    ref_path = model_dir / ref_name
    if not ref_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"参考音频文件不存在: {ref_path}（meta.json 字段 ref_audio={ref_name!r}）",
        )
    ext = ref_path.suffix.lstrip(".").lower() or "wav"
    media_types = {
        "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
        "opus": "audio/opus", "flac": "audio/flac", "m4a": "audio/mp4", "aac": "audio/aac",
    }
    return FileResponse(
        ref_path,
        media_type=media_types.get(ext, "application/octet-stream"),
        filename=f"{id}_ref.{ext}",
    )


# ============== 在线 catalog ==============


@router.get("/catalog", response_model=list[TTSModel])
async def list_catalog(
    force: bool = False,
    catalog: ModelCatalog = Depends(get_catalog),
):
    return await catalog.fetch(force=force)


@router.post("/catalog/refresh")
async def refresh_catalog(catalog: ModelCatalog = Depends(get_catalog)):
    """强制刷新 catalog 缓存（含 CDN 边缘缓存破坏），并返回刷新后的模型列表。"""
    try:
        models = await catalog.fetch(force=True)
        logger.info("catalog refreshed: models=%d", len(models))
        return {"ok": True, "models_count": len(models)}
    except ModelError as e:
        logger.warning("catalog refresh failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ============== 下载模型 (SSE) ==============


@router.post("/download/{id}")
async def download_model(id: str, catalog: ModelCatalog = Depends(get_catalog)):
    async def gen() -> AsyncIterator[str]:
        try:
            async for evt in catalog.download(id):
                yield _sse(evt)
        except (ModelError, ValidationError) as e:
            logger.warning("download model %s failed: %s", id, e)
            yield _sse({"phase": "error", "message": str(e), "model_id": id})
            return
        except Exception as e:
            logger.exception("download model %s unexpected error", id)
            yield _sse({"phase": "error", "message": f"未预期错误: {e}", "model_id": id})
            return
        invalidate_caches()
        logger.info("model downloaded: id=%s", id)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ============== 删除模型 ==============


@router.delete("/{id}")
def remove_model(id: str, store: ModelStore = Depends(get_model_store)):
    if not store.get(id):
        raise NotFoundError(f"模型不存在: {id}")
    store.remove(id)
    invalidate_caches()
    logger.info("model removed: id=%s", id)
    return {"ok": True, "id": id}


# ============== 导入本地模型（zip 上传）==============


@router.post("/import", response_model=TTSModel)
async def import_model(
    file: UploadFile = File(...),
    store: ModelStore = Depends(get_model_store),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise ValidationError("仅支持 .zip 文件上传")

    with tempfile.TemporaryDirectory(prefix="npc-model-import-") as td:
        td_path = Path(td)
        zip_path = td_path / "in.zip"
        with zip_path.open("wb") as f:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise ValidationError(f"zip 含非法路径: {name}")
                extract_dir = td_path / "extracted"
                extract_dir.mkdir()
                zf.extractall(extract_dir)
        except zipfile.BadZipFile as e:
            raise ValidationError(f"无效的 zip 文件: {e}") from e

        meta_files = list(extract_dir.rglob("meta.json"))
        if not meta_files:
            raise ValidationError("zip 中找不到 meta.json")
        if len(meta_files) > 1:
            raise ValidationError(f"zip 中含多个 meta.json (期望 1 个，实际 {len(meta_files)})")
        model_dir = meta_files[0].parent

        m = store.import_from_path(model_dir)

    invalidate_caches()
    logger.info("model imported: id=%s name=%s", m.id, m.name)
    return m


# ============== 本地 TTS 运行时 ==============


async def _fetch_latest_versions(catalog: ModelCatalog) -> dict[str, str]:
    """从 catalog manifest 读取每个 target 的最新版本号。

    返回 {target: version_str}；若段缺失则 version 为空字符串。
    """
    from tts.catalog_client import GithubReleaseCatalog

    if not isinstance(catalog, GithubReleaseCatalog):
        return {}
    try:
        manifest = await catalog.fetch_raw(force=False)
    except Exception:
        logger.warning("fetch_latest_versions: failed to read manifest")
        return {}
    out: dict[str, str] = {}
    for target in _VALID_TARGETS:
        key = f"windows_x64_{target.replace('-', '_')}"
        slot = manifest.get(key)
        if not isinstance(slot, dict):
            # cpu fallback to legacy "windows_x64"
            if target == "cpu":
                slot = manifest.get("windows_x64")
        ver = ""
        if isinstance(slot, dict):
            ver = str(slot.get("version") or manifest.get("version") or "")
        out[target] = ver
    return out


@router.get("/runtime/status")
async def runtime_status(
    catalog: ModelCatalog = Depends(get_catalog),
):
    """返回所有 target 的运行时安装状态数组。

    每项含 installed/version/latest_version/can_update/can_install/target。
    active_target 字段表示当前设置选中的活跃 target。
    """
    cfg = get_config()
    latest = await _fetch_latest_versions(catalog)
    targets = []
    for target in _VALID_TARGETS:
        inst = _make_runtime_installer(target)
        installed = inst.is_installed()
        ver = inst.installed_version()
        latest_ver = latest.get(target, "")
        can_update = False
        if installed and ver and latest_ver and ver != latest_ver:
            can_update = True
        targets.append({
            "name": inst.name,
            "target": target,
            "installed": installed,
            "version": ver,
            "latest_version": latest_ver,
            "can_update": can_update,
            "can_install": not installed and bool(latest_ver),
        })
    return {
        "targets": targets,
        "active_target": cfg.tts.local.target,
    }


@router.post("/runtime/install")
async def runtime_install(body: dict):
    """安装指定 target 的运行时。

    Body: {"target": "cpu"|"amd-rocm"|"nvidia-cuda"}
    不再因 target 不匹配自动 uninstall 旧变体；多 target 并存。
    """
    target = body.get("target", "cpu")
    if target not in _VALID_TARGETS:
        raise ValidationError(f"无效 target: {target}; 有效值: {_VALID_TARGETS}")

    inst = _make_runtime_installer(target)

    async def gen() -> AsyncIterator[str]:
        try:
            async for evt in inst.install():
                yield _sse(evt)
        except (ModelError, ValidationError) as e:
            logger.warning("runtime install failed: target=%s error=%s", target, e)
            yield _sse({"phase": "error", "message": str(e)})
            return
        except Exception as e:
            logger.exception("runtime install unexpected error: target=%s", target)
            yield _sse({"phase": "error", "message": f"未预期错误: {e}"})
            return
        invalidate_caches()
        logger.info("runtime installed: target=%s", target)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/runtime/uninstall")
def runtime_uninstall(body: dict):
    """卸载指定 target 的运行时。

    Body: {"target": "cpu"|"amd-rocm"|"nvidia-cuda"}
    """
    target = body.get("target", "cpu")
    if target not in _VALID_TARGETS:
        raise ValidationError(f"无效 target: {target}; 有效值: {_VALID_TARGETS}")

    inst = _make_runtime_installer(target)
    if not inst.is_installed():
        return {"ok": False, "message": f"运行时未安装（target={target}）"}
    inst.uninstall()
    invalidate_caches()
    logger.info("runtime uninstalled: target=%s", target)
    return {"ok": True, "target": target}
