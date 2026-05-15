# @purpose: 对话 HTTP 路由（CRUD + 批量导入）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/dialogues
#   - GET    /api/dialogues
#   - POST   /api/dialogues
#   - GET    /api/dialogues/{id}
#   - PATCH  /api/dialogues/{id}
#   - DELETE /api/dialogues/{id}
#   - POST   /api/dialogues/import     (multipart)
#   - POST   /api/dialogues/reorder    (body: {ids: [...]} 完整全量顺序)
#   - POST   /api/dialogues/bulk-patch (body: {ids: [...], patch: {...}})
# @depends:
#   - fastapi (APIRouter, Depends, Form, File, UploadFile, HTTPException)
#   - ../contract/models.py: Dialogue, Emotion
#   - ../contract/errors.py: NotFoundError, ValidationError, StorageError
#   - ../dialogue/service.py, ../dialogue/importer.py
#   - ../character/service.py: CharacterService
#   - ./deps.py
# @invariants:
#   - 导入接口接受 content (字符串粘贴) 或 file (上传)；file 优先
#   - format 取值：screenplay | csv | json | lines；其他值返回 400
#   - auto_create=True 时未知角色名自动建档（默认音色），实现"粘贴剧本即生成"的核心流程
#   - scene 表单字段统一写入本批所有对话的 Dialogue.scene
#   - reorder 要求传入的 ids 是当前全部对话的一个排列；bulk-patch 仅允许改 scene/emotion/character_id/filename

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from character.service import CharacterService
from contract.errors import NotFoundError, StorageError, ValidationError
from contract.models import Dialogue, Emotion
from dialogue import importer
from dialogue.service import DialogueService

from .deps import get_character_service, get_dialogue_service

router = APIRouter(prefix="/api/dialogues", tags=["dialogues"])


@router.get("", response_model=list[Dialogue])
def list_dialogues(svc: DialogueService = Depends(get_dialogue_service)):
    return svc.list()


@router.post("", response_model=Dialogue, status_code=201)
def create_dialogue(data: dict, svc: DialogueService = Depends(get_dialogue_service)):
    try:
        return svc.create(data)
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.post("/import")
async def batch_import(
    format: str = Form(...),
    content: str = Form(""),
    file: Optional[UploadFile] = File(None),
    default_character_id: str = Form(""),
    default_emotion: str = Form("neutral"),
    scene: str = Form(""),
    auto_create: bool = Form(True),
    dlg_svc: DialogueService = Depends(get_dialogue_service),
    char_svc: CharacterService = Depends(get_character_service),
):
    if file is not None:
        body = await file.read()
        content = body.decode("utf-8", errors="replace")
    if not content.strip():
        raise HTTPException(400, "content is empty")

    try:
        default_emo = Emotion(default_emotion)
    except ValueError:
        default_emo = Emotion.NEUTRAL

    created_names: list[str] = []

    def resolver(name: str) -> str:
        name = (name or "").strip()
        if not name:
            return ""
        existing = char_svc.find_by_name(name)
        if existing:
            return existing.id
        if auto_create:
            character = char_svc.create({"name": name})
            created_names.append(name)
            return character.id
        return ""

    try:
        if format == "screenplay":
            dialogues = importer.parse_screenplay(content, resolver, default_emo, scene)
        elif format == "csv":
            dialogues = importer.parse_csv(content, resolver, default_character_id, default_emo, scene)
        elif format == "json":
            dialogues = importer.parse_json(content, resolver, default_character_id, default_emo, scene)
        elif format == "lines":
            dialogues = importer.parse_text(content, default_character_id, default_emo, scene)
        else:
            raise HTTPException(400, f"unknown format: {format}")
    except ValidationError as e:
        raise HTTPException(400, str(e))

    count = dlg_svc.bulk_add(dialogues)
    return {"imported": count, "created_characters": created_names}


@router.get("/{id}", response_model=Dialogue)
def get_dialogue(id: str, svc: DialogueService = Depends(get_dialogue_service)):
    try:
        return svc.get(id)
    except NotFoundError as e:
        raise HTTPException(404, str(e))


@router.patch("/{id}", response_model=Dialogue)
def update_dialogue(id: str, patch: dict, svc: DialogueService = Depends(get_dialogue_service)):
    try:
        return svc.update(id, patch)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.delete("/{id}")
def delete_dialogue(id: str, svc: DialogueService = Depends(get_dialogue_service)):
    try:
        svc.delete(id)
        return {"ok": True}
    except NotFoundError as e:
        raise HTTPException(404, str(e))


@router.post("/reorder")
def reorder_dialogues(data: dict, svc: DialogueService = Depends(get_dialogue_service)):
    ids = data.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "ids must be a list of dialogue id strings")
    try:
        svc.reorder(ids)
    except StorageError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "count": len(ids)}


@router.post("/bulk-patch")
def bulk_patch_dialogues(data: dict, svc: DialogueService = Depends(get_dialogue_service)):
    ids = data.get("ids") or []
    patch = data.get("patch") or {}
    if not isinstance(ids, list) or not isinstance(patch, dict):
        raise HTTPException(400, "ids:list and patch:dict required")
    return {"updated": svc.bulk_patch(ids, patch)}
