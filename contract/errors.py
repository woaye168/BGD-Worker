# @purpose: 领域错误层次（与 HTTP 状态码解耦）
# @layer: contract
# @contract:
#   - DomainError (基类)
#   - NotFoundError, ValidationError, TTSError, StorageError
# @depends:
#   - 无
# @invariants:
#   - HTTP 状态映射在 api 层完成，logic / adapter 仅 raise 领域错误
#   - 所有领域错误继承自 DomainError，便于统一捕获

class DomainError(Exception):
    """领域错误基类。"""


class NotFoundError(DomainError):
    """实体不存在。映射 HTTP 404。"""


class ValidationError(DomainError):
    """输入校验失败。映射 HTTP 400。"""


class TTSError(DomainError):
    """TTS 引擎或音频转码失败。映射 HTTP 502。"""


class StorageError(DomainError):
    """持久化层故障。映射 HTTP 500。"""
