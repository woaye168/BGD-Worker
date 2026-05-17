# @purpose: 模型管理 HTTP 路由（已安装/在线 catalog/SSE 下载/zip 导入/运行时安装卸载）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/models
#   - GET    /api/models/installed              已安装模型列表
#   - GET    /api/models/catalog?force=         在线 catalog（缓存 ttl 内不重拉）
#   - DELETE /api/models/{id}                   删除已安装模型
#   - POST   /api/models/import                 multipart zip 上传 → 解压 → 导入
#   - POST   /api/models/download/{id}          SSE 流式下载（在 catalog 中找到 id）
#   - GET    /api/models/runtime/status         本地 TTS 运行时安装状态
#   - POST   /api/models/runtime/install        SSE 流式安装运行时（仅 win32）
#   - POST   /api/models/runtime/uninstall      删除已安装运行时
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
#   - 路由前缀 /api/models 与现有 /api/models/* 不冲突（新增）

from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from contract.errors import ModelError, NotFoundError, ValidationError
from contract.models import TTSModel
from contract.ports import ModelCatalog, ModelStore, RuntimeInstaller

from .deps import (
    get_catalog,
    get_model_store,
    get_runtime_installer,
    invalidate_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ============== 已安装模型 ==============


@router.get("/installed", response_model=list[TTSModel])
def list_installed(store: ModelStore = Depends(get_model_store)):
    return store.list_installed()


# ============== 在线 catalog ==============


@router.get("/catalog", response_model=list[TTSModel])
async def list_catalog(
    force: bool = False,
    catalog: ModelCatalog = Depends(get_catalog),
):
    return await catalog.fetch(force=force)


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


@router.get("/runtime/status")
def runtime_status(inst: RuntimeInstaller = Depends(get_runtime_installer)):
    """返回运行时安装状态。

    target 字段 = 当前设置选择的 target；
    installed_target = 实际已装的 target（旧 runtime VERSION 仅含版本号时默认 cpu）。
    两者不一致时前端可提示"切换后端需重装"。
    """
    installed_target = (
        inst.installed_target() if hasattr(inst, "installed_target") else None
    )
    return {
        "name": inst.name,
        "installed": inst.is_installed(),
        "version": inst.installed_version(),
        "target": getattr(inst, "target", "cpu"),
        "installed_target": installed_target,
        "target_mismatch": (
            inst.is_installed()
            and installed_target is not None
            and installed_target != getattr(inst, "target", "cpu")
        ),
    }


@router.post("/runtime/install")
async def runtime_install(inst: RuntimeInstaller = Depends(get_runtime_installer)):
    async def gen() -> AsyncIterator[str]:
        try:
            # 已装但 target 不匹配（用户切了后端） → 先卸载老变体，再装新变体
            installed_target = (
                inst.installed_target() if hasattr(inst, "installed_target") else None
            )
            target = getattr(inst, "target", "cpu")
            if inst.is_installed() and installed_target and installed_target != target:
                yield _sse({
                    "phase": "start",
                    "message": f"检测到已装 target={installed_target}，与当前选择 {target} 不一致，先卸载老变体...",
                    "target": target,
                })
                inst.uninstall()
            async for evt in inst.install():
                yield _sse(evt)
        except (ModelError, ValidationError) as e:
            logger.warning("runtime install failed: %s", e)
            yield _sse({"phase": "error", "message": str(e)})
            return
        except Exception as e:
            logger.exception("runtime install unexpected error")
            yield _sse({"phase": "error", "message": f"未预期错误: {e}"})
            return
        invalidate_caches()
        logger.info("runtime installed")

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/runtime/uninstall")
def runtime_uninstall(inst: RuntimeInstaller = Depends(get_runtime_installer)):
    if not inst.is_installed():
        return {"ok": False, "message": "运行时未安装"}
    inst.uninstall()
    invalidate_caches()
    logger.info("runtime uninstalled")
    return {"ok": True}
