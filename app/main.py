"""FastAPI 入口：快递理赔时限判定 HTTP API。"""
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import db
from .errors import AppError
from .schemas import (ClaimApplicationRequest, CreateOrderRequest, EventBatchRequest,
                      EventIn, EventPreviewRequest)
from . import claims
from .services import (archive_determination, create_order, determination_history,
                       ingest_events, list_events, live_determination, preview_events)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    yield


app = FastAPI(
    title="快递理赔时限判定服务",
    version="1.1.0",
    description="按订单创建时生效政策版本判定理赔申请时限；支持时区/节假日、事件去重、"
                "迟到更正、代际归档，以及理赔申请受理与不可变凭证（幂等）。",
    lifespan=lifespan,
)


# ------------------------------------------------------------ 统一错误格式

@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"] if x not in ("body",))
        details.append({
            "field": loc or "body",
            "type": err["type"],
            "message": err["msg"],
        })
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "INVALID_REQUEST",
            "message": "请求参数校验失败（字段缺失或时间缺少时区等）",
            "details": details,
        }},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------ 订单

@app.post("/api/v1/orders", status_code=201)
def api_create_order(req: CreateOrderRequest):
    return create_order(req)


@app.get("/api/v1/orders/{order_id}")
def api_get_order(order_id: str):
    from .services import require_order
    return require_order(order_id)


# ------------------------------------------------------------ 事件

@app.post("/api/v1/orders/{order_id}/events")
def api_ingest_event(order_id: str, req: EventIn):
    return ingest_events(order_id, [req])


@app.post("/api/v1/orders/{order_id}/events/batch")
def api_ingest_batch(order_id: str, req: EventBatchRequest):
    return ingest_events(order_id, req.events)


@app.post("/api/v1/orders/{order_id}/events/preview")
def api_preview_events(order_id: str, req: EventPreviewRequest):
    """无写入预演：在当前最新代事件视图上模拟整批摄取（含更正规则），
    返回逐项校验结果、首个失败位置、模拟判定与判定依据差异；
    不写事件、不开新代、不改归档快照。已归档订单按新一代结果计算。"""
    return preview_events(order_id, req.events, as_of=req.as_of)


@app.get("/api/v1/orders/{order_id}/events")
def api_list_events(order_id: str):
    return list_events(order_id)


# ------------------------------------------------------------ 判定

def _parse_as_of(v: str | None) -> datetime | None:
    if not v:
        return None
    from .engine import parse_dt
    try:
        return parse_dt(v)
    except Exception:
        raise AppError("INVALID_AS_OF", "as_of 必须是带时区偏移的 ISO8601 时间", 422)


@app.get("/api/v1/orders/{order_id}/determination")
def api_live(order_id: str, as_of: str | None = None):
    """实时判定（最新一代未归档则现算；已归档则返回不可变快照）。"""
    return live_determination(order_id, as_of=_parse_as_of(as_of))


@app.post("/api/v1/orders/{order_id}/determination/archive")
def api_archive(order_id: str, as_of: str | None = None):
    """归档当前代判定。归档后迟到事件只会另开新一代，不改写本快照。"""
    return archive_determination(order_id, as_of=_parse_as_of(as_of))


@app.get("/api/v1/orders/{order_id}/determinations")
def api_history(order_id: str):
    return determination_history(order_id)


# ------------------------------------------------------------ 理赔申请受理（凭证）

@app.post("/api/v1/orders/{order_id}/claims")
def api_submit_claim(order_id: str, req: ClaimApplicationRequest,
                     response: Response,
                     idempotency_key: str | None = Header(default=None)):
    """按订单与理赔类型提交理赔申请。

    - 以服务实际接收时间（received_at，缺省=服务当前 UTC）核验当前代有效事件、
      下单锁定政策与当地截止时刻；当前代已归档也按接收时间重新核验，不采用归档快照；
    - 仅该类型为 filable 才受理并返回不可变凭证；否则返回可区分的业务拒绝码，不生成凭证；
    - 幂等键取请求体 idempotency_key 或 Idempotency-Key 头：
      同订单同键同类型重试返回原凭证（200, idempotent_replay=true）；
      同订单同键换类型 => 409 IDEMPOTENCY_KEY_CONFLICT；
      同订单同类型换键重复受理 => 409 CLAIM_ALREADY_ACCEPTED。
    """
    from .services import require_order
    require_order(order_id)
    key = req.idempotency_key or idempotency_key
    voucher, replay = claims.submit_claim(order_id, req, key)
    response.status_code = 200 if replay else 201
    return voucher


@app.get("/api/v1/orders/{order_id}/claims")
def api_list_claims(order_id: str,
                    claim_type: str | None = None,
                    determination_code: str | None = None):
    """查询订单下的受理凭证；可按理赔类型 / 判定代号过滤。"""
    return claims.list_applications(order_id, claim_type=claim_type,
                                    determination_code=determination_code)


@app.get("/api/v1/orders/{order_id}/claims/{application_id}")
def api_get_claim(order_id: str, application_id: str):
    """查询单张不可变凭证及其所依据的判定代号/判定依据。"""
    return claims.get_application(order_id, application_id)


# ------------------------------------------------------------ 基础数据

@app.get("/api/v1/policies")
def api_policies():
    rows = db.q("SELECT version, valid_from, valid_to, body_json FROM policies ORDER BY valid_from")
    return {"policies": [
        {"version": r["version"], "valid_from": r["valid_from"],
         "valid_to": r["valid_to"], "body": json.loads(r["body_json"])}
        for r in rows
    ]}


@app.get("/api/v1/calendars/{calendar_code}")
def api_calendar(calendar_code: str):
    rows = db.q(
        "SELECT date, kind, name FROM calendars WHERE calendar_code = ? ORDER BY date",
        (calendar_code,),
    )
    if not rows:
        raise AppError("CALENDAR_NOT_FOUND", f"节假日表不存在: {calendar_code}", 404)
    return {"calendar_code": calendar_code, "days": [dict(r) for r in rows]}
