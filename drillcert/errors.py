"""统一的领域错误类型。

所有业务错误都携带稳定的机器可读 code，HTTP 层据此映射状态码，
避免把堆栈或内部细节泄漏给调用方。
"""


class DomainError(Exception):
    """领域规则违例基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self):
        return {"error": self.code, "message": self.message}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class ForbiddenError(DomainError):
    code = "forbidden"
    http_status = 403


class PayloadError(DomainError):
    """请求体无法解析（非 JSON 等）。"""

    code = "bad_request"
    http_status = 400
