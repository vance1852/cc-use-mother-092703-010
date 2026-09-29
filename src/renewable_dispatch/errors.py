"""联合消纳服务向 API 和 CLI 暴露的稳定错误。"""


class DispatchError(RuntimeError):
    code = "dispatch_error"
    status = 400


class NotFound(DispatchError):
    code = "not_found"
    status = 404


class Conflict(DispatchError):
    code = "conflict"
    status = 409


class Forbidden(DispatchError):
    code = "forbidden"
    status = 403


class InvalidState(DispatchError):
    code = "invalid_state"
    status = 409


class ValidationFailed(DispatchError):
    code = "validation_failed"
    status = 422
