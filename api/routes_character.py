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
#   - POST   /api/characters/audition         (任意文本+音色试听，不依赖角色/对话)
# @depends:
#   - fastapi (APIRouter, Depends, HTTPException, Response)
#   - ../contract/models.py: Character, Emotion
#   - ../contract/errors.py: NotFoundError, ValidationError, TTSError
#   - ../contract/ports.py: TTSEngine
#   - ../character/service.py: CharacterService
#   - ./deps.py: get_character_service, get_tts_engine
# @invariants:
#   - 领域错误到 HTTP 状态映射在此层完成：NotFound→404, Validation→400, TTS→502
#   - 不直接持有 repository，仅通过 service 调用
#   - audition 不持久化，仅返回音频字节，供分配音色前试听

from fastapi import APIRouter, Depends, HTTPException, Response

from character.service import CharacterService
from contract.errors import NotFoundError, TTSError, ValidationError
from contract.models import Character, Emotion
from contract.ports import TTSEngine

from .deps import get_character_service, get_tts_engine

router = APIRouter(prefix="/api/characters", tags=["characters"])

_MEDIA_TYPES = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}


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


@router.post("/audition")
async def audition(data: dict, tts: TTSEngine = Depends(get_tts_engine)):
    voice = (data.get("voice") or "").strip()
    if not voice:
        raise HTTPException(400, "voice required")
    text = (data.get("text") or "你好，旅行者，欢迎来到这片土地。").strip()
    emotion_raw = data.get("emotion", "neutral")
    try:
        emotion = Emotion(emotion_raw)
    except ValueError:
        emotion = Emotion.NEUTRAL
    try:
        audio = await tts.synthesize(
            text=text,
            voice=voice,
            emotion=emotion,
            rate=float(data.get("rate", 1.0)),
            pitch=float(data.get("pitch", 1.0)),
            volume=float(data.get("volume", 1.0)),
        )
    except TTSError as e:
        raise HTTPException(502, str(e))
    media = _MEDIA_TYPES.get(tts.output_extension, "application/octet-stream")
    return Response(content=audio, media_type=media)


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
