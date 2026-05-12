# @purpose: 角色 HTTP 路由（CRUD + 列出可用语音）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/characters
#   - GET    /api/characters
#   - POST   /api/characters
#   - GET    /api/characters/{id}
#   - PATCH  /api/characters/{id}
#   - DELETE /api/characters/{id}
#   - GET    /api/characters/voices/available
# @depends:
#   - fastapi (APIRouter, Depends, HTTPException)
#   - ../contract/models.py: Character
#   - ../contract/errors.py: NotFoundError, ValidationError
#   - ../character/service.py: CharacterService
#   - ./deps.py: get_character_service, get_tts_engine
# @invariants:
#   - 领域错误到 HTTP 状态映射在此层完成：NotFound→404, Validation→400
#   - 不直接持有 repository，仅通过 service 调用

from fastapi import APIRouter, Depends, HTTPException

from character.service import CharacterService
from contract.errors import NotFoundError, ValidationError
from contract.models import Character
from contract.ports import TTSEngine

from .deps import get_character_service, get_tts_engine

router = APIRouter(prefix="/api/characters", tags=["characters"])


@router.get("", response_model=list[Character])
def list_characters(svc: CharacterService = Depends(get_character_service)):
    return svc.list()


@router.post("", response_model=Character, status_code=201)
def create_character(data: dict, svc: CharacterService = Depends(get_character_service)):
    try:
        return svc.create(data)
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.get("/voices/available")
async def list_voices(tts: TTSEngine = Depends(get_tts_engine)):
    return await tts.list_voices()


@router.get("/{id}", response_model=Character)
def get_character(id: str, svc: CharacterService = Depends(get_character_service)):
    try:
        return svc.get(id)
    except NotFoundError as e:
        raise HTTPException(404, str(e))


@router.patch("/{id}", response_model=Character)
def update_character(id: str, patch: dict, svc: CharacterService = Depends(get_character_service)):
    try:
        return svc.update(id, patch)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.delete("/{id}")
def delete_character(id: str, svc: CharacterService = Depends(get_character_service)):
    try:
        svc.delete(id)
        return {"ok": True}
    except NotFoundError as e:
        raise HTTPException(404, str(e))
