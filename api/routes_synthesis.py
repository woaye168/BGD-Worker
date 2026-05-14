# @purpose: 合成 HTTP 路由（批量/单条合成、试听、SSE 进度、ZIP 导出）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/synthesis
#   - POST /api/synthesis/batch              (同步批量，返回所有结果)
#   - POST /api/synthesis/batch/stream       (SSE 流式进度，客户端断连即中止)
#   - POST /api/synthesis/one/{id}           (单条合成并持久化，返回 SynthesisResult)
#   - POST /api/synthesis/preview/{id}       (单条实时合成，返回音频字节，不持久化)
#   - GET  /api/synthesis/audio/{id}         (试听已合成的对话音频文件)
#   - POST /api/synthesis/export             (按范围打包已合成音频为 ZIP)
# @depends:
#   - fastapi (APIRouter, Depends, HTTPException, Response, FileResponse, StreamingResponse)
#   - json (stdlib)
#   - ../contract/models.py: SynthesisRequest, SynthesisResult
#   - ../contract/ports.py: AudioStore, CharacterRepository, TTSEngine
#   - ../contract/errors.py
#   - ../synthesis/orchestrator.py, ../synthesis/exporter.py
#   - ../dialogue/service.py
#   - ./deps.py
# @invariants:
#   - preview 不写 audio_store / 不修改对话；仅用于试听未合成内容
#   - one 端点等价于 batch 的单条版本，必持久化
#   - audio 端点返回已持久化文件，404 当 dialogue.audio_path 为空或文件丢失
#   - 音频 media_type 由扩展名映射；扩展名取自 audio_path（已合成）或引擎 output_extension（实时）
#   - SSE 事件格式：每行 "data: <json>\n\n"；phase ∈ start/progress/done
#   - batch/stream 的中止由客户端断开连接实现，服务端无显式取消状态
#   - export 仅打包 audio_path 有效的对话，未合成项静默跳过

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse

from contract.errors import DomainError, NotFoundError, TTSError
from contract.models import SynthesisRequest, SynthesisResult
from contract.ports import AudioStore, CharacterRepository, TTSEngine
from dialogue.service import DialogueService
from synthesis.exporter import build_zip
from synthesis.orchestrator import SynthesisOrchestrator

from .deps import (
    get_audio_store,
    get_character_repo,
    get_dialogue_service,
    get_orchestrator,
    get_tts_engine,
)

router = APIRouter(prefix="/api/synthesis", tags=["synthesis"])

_MEDIA_TYPES = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}


def _media_type(ext: str) -> str:
    return _MEDIA_TYPES.get(ext.lower(), "application/octet-stream")


@router.post("/batch")
async def batch_synthesize(
    req: SynthesisRequest,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
):
    targets = orch.select(req.scope, req.dialogue_ids)
    results: list[SynthesisResult] = []
    async for r in orch.batch(targets):
        results.append(r)
    return {
        "total": len(targets),
        "success": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "results": [r.model_dump() for r in results],
    }


@router.post("/batch/stream")
async def batch_stream(
    req: SynthesisRequest,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
):
    targets = orch.select(req.scope, req.dialogue_ids)

    async def event_gen() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'total': len(targets), 'phase': 'start'})}\n\n"
        idx = 0
        async for r in orch.batch(targets):
            idx += 1
            payload = {
                "phase": "progress",
                "index": idx,
                "total": len(targets),
                "result": r.model_dump(),
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'phase': 'done'})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/one/{dialogue_id}", response_model=SynthesisResult)
async def synthesize_one(
    dialogue_id: str,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
    dlg_svc: DialogueService = Depends(get_dialogue_service),
):
    try:
        d = dlg_svc.get(dialogue_id)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    return await orch.synthesize_one(d)


@router.post("/preview/{dialogue_id}")
async def preview(
    dialogue_id: str,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
    dlg_svc: DialogueService = Depends(get_dialogue_service),
    tts: TTSEngine = Depends(get_tts_engine),
):
    try:
        d = dlg_svc.get(dialogue_id)
        audio = await orch.render(d)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except TTSError as e:
        raise HTTPException(502, str(e))
    except DomainError as e:
        raise HTTPException(500, str(e))
    return Response(content=audio, media_type=_media_type(tts.output_extension))


@router.get("/audio/{dialogue_id}")
def get_audio(
    dialogue_id: str,
    dlg_svc: DialogueService = Depends(get_dialogue_service),
    store: AudioStore = Depends(get_audio_store),
):
    try:
        d = dlg_svc.get(dialogue_id)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    if not d.audio_path or not store.exists(d.audio_path):
        raise HTTPException(404, "audio not generated yet")
    ext = d.audio_path.rsplit(".", 1)[-1] if "." in d.audio_path else "bin"
    download_name = f"{d.filename or dialogue_id}.{ext}"
    return FileResponse(
        store.absolute(d.audio_path),
        media_type=_media_type(ext),
        filename=download_name,
    )


@router.post("/export")
def export_zip(
    req: SynthesisRequest,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
    char_repo: CharacterRepository = Depends(get_character_repo),
    store: AudioStore = Depends(get_audio_store),
):
    targets = orch.select(req.scope, req.dialogue_ids)
    characters_by_id = {c.id: c for c in char_repo.list()}
    data = build_zip(targets, characters_by_id, store)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="npc_voices.zip"'},
    )
