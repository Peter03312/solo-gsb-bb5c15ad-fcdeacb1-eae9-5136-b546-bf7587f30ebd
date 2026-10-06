"""统一的业务错误类型与错误码。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


ERR_ORDER_NOT_FOUND = ("ORDER_NOT_FOUND", 404)
ERR_EVENT_ORDER_INVALID = ("EVENT_ORDER_INVALID", 422)
ERR_CORRECTION_TARGET_NOT_FOUND = ("CORRECTION_TARGET_NOT_FOUND", 422)
ERR_POLICY_NOT_FOUND = ("POLICY_NOT_FOUND", 422)
ERR_CALENDAR_NOT_FOUND = ("CALENDAR_NOT_FOUND", 422)
ERR_ALREADY_ARCHIVED = ("ALREADY_ARCHIVED", 409)
ERR_IDEMPOTENCY_KEY_CONFLICT = ("IDEMPOTENCY_KEY_CONFLICT", 409)
ERR_CLAIM_ALREADY_ACCEPTED = ("CLAIM_ALREADY_ACCEPTED", 409)
ERR_CLAIM_NOT_FOUND = ("CLAIM_NOT_FOUND", 404)

# 理赔申请受理——业务拒绝原因（可区分；不生成凭证）
ERR_CLAIM_PENDING = ("CLAIM_PENDING", 422)              # 触发事件尚未出现
ERR_CLAIM_NOT_FILABLE = ("CLAIM_NOT_FILABLE", 422)      # 已过当地截止时刻
ERR_CLAIM_NOT_APPLICABLE = ("CLAIM_NOT_APPLICABLE", 422)  # 理赔事由不成立
ERR_CLAIM_INDETERMINATE = ("CLAIM_INDETERMINATE", 422)  # 证据不足，不可判定
