# @purpose: 应用设置 HTTP 路由（读取/更新；变更后重置依赖单例 + 重新初始化日志）
# @layer: adapter
# @contract:
#   - router: APIRouter prefix=/api/settings
#   - GET /api/settings        → 当前生效设置（含派生路径，只读字段）
#   - PUT /api/settings        body: 部分字段 → 深合并、校验、保存、应用
# @depends:
#   - logging (stdlib)
#   - fastapi (APIRouter, HTTPException)
#   - pydantic.ValidationError
#   - ../contract/config.py: AppConfig
#   - ./deps.py: get_config, invalidate_caches
#   - ./logging_setup.py: setup_logging
# @invariants:
#   - PUT 行为是"深合并 + 整体校验"：嵌套子对象可仅传变更字段
#   - 持久化通过 AppConfig.save()；data_dir 不可由 API 改（启动期/环境变量决定）
#   - 应用变更顺序固定：save → invalidate_caches → setup_logging（先持久化后副作用）
#   - 响应包含 data_dir/audio_dir/db_file/log_dir 等派生路径，供前端展示但不可直接写

import logging

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError as PydanticValidationError

from contract.config import AppConfig

from .deps import get_config, invalidate_caches
from .logging_setup import setup_logging

router = APIRouter(prefix="/api/settings", tags=["settings"])
_logger = logging.getLogger("npc.settings")


def _response(cfg: AppConfig) -> dict:
    return {
        "data_dir": str(cfg.data_dir),
        "audio_dir": str(cfg.audio_dir),
        "db_file": str(cfg.db_file),
        "log_dir": str(cfg.log_dir),
        "settings": cfg.model_dump(mode="json", exclude={"data_dir"}),
    }


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@router.get("")
def read_settings() -> dict:
    return _response(get_config())


@router.put("")
def update_settings(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise HTTPException(400, "请求体必须是对象")
    patch.pop("data_dir", None)  # 不允许通过 API 改 data_dir

    cfg = get_config()
    current = cfg.model_dump(mode="json", exclude={"data_dir"})
    merged = _deep_merge(current, patch)
    try:
        new_cfg = AppConfig.model_validate({**merged, "data_dir": cfg.data_dir})
    except PydanticValidationError as e:
        raise HTTPException(400, f"设置校验失败：{e.errors()}")

    new_cfg.save()
    invalidate_caches()
    setup_logging(new_cfg.log, new_cfg.log_dir)
    _logger.info("settings updated: %s", list(patch.keys()))
    return _response(new_cfg)
