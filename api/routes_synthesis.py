# @purpose: 合成 HTTP 路由（批量合成 / 实时预览 / 试听已合成 / SSE 进度）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/synthesis
#   - POST /api/synthesis/batch              (同步批量，返回所有结果)
#   - POST /api/synthesis/batch/stream       (SSE 流式进度)
#   - POST /api/synthesis/preview/{id}       (单条实时合成，返回 OGG 字节，不持久化)
#   - GET  /api/synthesis/audio/{id}         (试听已合成的对话音频文件)
# @depends:
#   - fastapi (APIRouter, Depends, HTTPException, Response, FileResponse, StreamingResponse)
#   - json (stdlib)
#   - ../contract/models.py: SynthesisRequest, SynthesisScope
#   - ../contract/errors.py
#   - ../synthesis/orchestrator.py
#   - ../dialogue/service.py
#   - ./deps.py
# @invariants:
#   - preview 不写 audio_store / 不修改对话；仅用于试听未合成内容
#   - audio 端点返回已持久化文件，404 当 dialogue.audio_path 为空或文件丢失
#   - SSE 事件格式：每行 "data: <json>\n\n"；首事件 {"total": N}，末事件 {"done": true}
#   - batch 端点同步等待所有合成完成才返回，长任务建议用 /batch/stream

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse

from contract.errors import DomainError, NotFoundError, TTSError
from contract.models import SynthesisRequest, SynthesisResult
from contract.ports import AudioStore
from dialogue.service import DialogueService
from synthesis.orchestrator import SynthesisOrchestrator

from .deps import get_audio_store, get_dialogue_service, get_orchestrator

router = APIRouter(prefix="/api/synthesis", tags=["synthesis"])


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


@router.post("/preview/{dialogue_id}")
async def preview(
    dialogue_id: str,
    orch: SynthesisOrchestrator = Depends(get_orchestrator),
    dlg_svc: DialogueService = Depends(get_dialogue_service),
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
    return Response(content=audio, media_type="audio/ogg")


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
    download_name = (d.filename or f"{dialogue_id}") + ".ogg"
    return FileResponse(
        store.absolute(d.audio_path),
        media_type="audio/ogg",
        filename=download_name,
    )
