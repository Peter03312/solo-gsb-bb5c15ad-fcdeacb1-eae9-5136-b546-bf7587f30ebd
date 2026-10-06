"""理赔申请受理服务。

规则（本轮新增，不改动既有事件/预演/归档接口）：

1. 客户按订单 + 理赔类型提交申请，以**服务实际接收时间** `received_at` 为核验基准：
   重新核验该订单当前代的有效扫描事件、下单时锁定的政策版本与当地截止时刻。
2. 即使当前代已归档，也按接收时间**重新核验**，不把较早的归档快照当作实时资格；
   归档快照本身原样保留、不受任何影响。
3. 仅当该类型状态为 `filable` 才受理，生成**不可变凭证**（含申请编号、受理时间、
   判定代号、适用政策、截止时间与当时判定依据）；否则返回**可区分的业务拒绝原因**，
   不生成凭证。
4. 幂等：同订单同键同类型重试返回原凭证（或原拒绝）；同订单同键换类型 => 409；
   同订单同类型至多一张凭证，并发、不同键也不得双重受理（唯一约束 + 串行写事务）。
5. 凭证及其判定依据一经受理不可变；后续更正或新代只影响之后的判定，已出凭证不变。
"""
import json
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime

from . import constants as C
from . import db
from .db import q1
from .engine import evaluate, parse_dt, to_local, utc_now
from .errors import AppError
from .schemas import ClaimApplicationRequest
from .services import _events_until_gen, _latest_gen, _load_eval_inputs

# 非 filable 状态 -> 可区分的业务拒绝码与说明
REJECTION_BY_STATUS = {
    "pending": (
        "CLAIM_PENDING",
        "该理赔类型的触发事件尚未出现，资格待定，暂不受理",
    ),
    "not_filable": (
        "CLAIM_NOT_FILABLE",
        "该理赔类型已过申请截止时间，不再受理",
    ),
    "not_applicable": (
        "CLAIM_NOT_APPLICABLE",
        "该理赔类型的理赔事由不成立，不予受理",
    ),
    "indeterminate": (
        "CLAIM_INDETERMINATE",
        "该理赔类型证据不足、当前不可判定，暂不受理",
    ),
}

# 判定代号形如 DELAY_FILABLE / DAMAGE_NOT_FILABLE，随凭证持久化、可按代号查询
CODE_PREFIX = {C.DELAY: "DELAY", C.DAMAGE: "DAMAGE", C.LOSS: "LOSS"}

IMMUTABLE_NOTE = "凭证及判定依据一经受理不可变；后续更正或新一代判定不影响本凭证"
RECHECK_ARCHIVED_NOTE = ("核验时当前第 {gen} 代已归档：已按服务接收时间基于该代有效事件"
                        "重新核验，未采用较早的归档快照作为实时资格")


# ------------------------------------------------------------ 凭证装配

def _policy_snapshot(policy: dict, claim_type: str) -> dict:
    rule = next(r for r in policy["claims"] if r["claim_type"] == claim_type)
    window = rule["window"]
    return {
        "version": policy["version"],
        "display_name": policy.get("display_name", policy["version"]),
        "locked_at_order_creation": True,
        "window": {
            "days": window["days"],
            "day_basis": window.get("day_basis", "calendar"),
            "cutoff": window.get("cutoff") or policy.get("default_cutoff", "23:59:59"),
        },
    }


def _build_voucher(*, application_id, order, claim_type, idem_key, received_utc,
                   accepted_utc, determination, claim, policy, archived) -> dict:
    """从实时核验结果装配不可变凭证。claim 必为 filable。"""
    tzname = order["timezone"]
    basis = list(claim["basis"])
    basis.append(f"服务实际接收时间：{to_local(received_utc, tzname).isoformat()}，"
                 f"资格以此为准（截止时刻同口径比较）")
    if archived:
        basis.append(RECHECK_ARCHIVED_NOTE.format(gen=determination["generation_no"]))
    basis.append(IMMUTABLE_NOTE)
    return {
        "application_id": application_id,
        "order_id": order["order_id"],
        "claim_type": claim_type,
        "claim_name": claim["claim_name"],
        "idempotency_key": idem_key,
        "status": "accepted",
        "received_at_utc": received_utc.isoformat(),
        "received_at_local": to_local(received_utc, tzname).isoformat(),
        "accepted_at_utc": accepted_utc.isoformat(),
        "accepted_at_local": to_local(accepted_utc, tzname).isoformat(),
        "determination_code": f"{CODE_PREFIX[claim_type]}_FILABLE",
        "applied_policy": _policy_snapshot(policy, claim_type),
        "policy_version": policy["version"],
        "generation_no": determination["generation_no"],
        "generation_archived_at_receipt": archived,
        "deadline_local": claim["deadline_local"],
        "deadline_utc": claim["deadline_utc"],
        "basis": basis,
        "determination_basis": {
            # 当时判定依据：顶层依据 + 该类型逐条依据 + 该类型证据，全部随凭证固化
            "top_level": determination["basis"],
            "claim_type": claim_type,
            "claim_status": claim["status"],
            "claim_basis": list(claim["basis"]),
            "anchor_time_utc": claim.get("anchor_time"),
            "evidence": claim.get("evidence", []),
            "missing_evidence": claim.get("missing_evidence", []),
        },
    }


def _rejection_payload(*, order, claim_type, idem_key, received_utc,
                       determination, claim, status) -> AppError:
    code, message = REJECTION_BY_STATUS[status]
    return AppError(code, message, 422, {
        "order_id": order["order_id"],
        "claim_type": claim_type,
        "claim_name": claim["claim_name"],
        "idempotency_key": idem_key,
        "received_at_utc": received_utc.isoformat(),
        "determination_code": f"{CODE_PREFIX[claim_type]}_{status.upper()}",
        "policy_version": determination["policy_version"],
        "generation_no": determination["generation_no"],
        "deadline_local": claim.get("deadline_local"),
        "deadline_utc": claim.get("deadline_utc"),
        "basis": claim.get("basis", []),
    })


# ------------------------------------------------------------ 幂等记录

def _replay_rejection(row: sqlite3.Row) -> AppError:
    rej = json.loads(row["rejection_json"])
    # 拒绝同样幂等：重试拿到与首次完全一致的错误体
    return AppError(rej["code"], rej["message"], rej.get("status_code", 422), rej["details"])


def _persist_rejection(conn, *, order_id, claim_type, idem_key, received_utc,
                       created_utc, exc: AppError) -> None:
    conn.execute(
        """INSERT INTO claim_idempotency(order_id, idempotency_key, claim_type, outcome,
                                         application_id, rejection_json, received_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (order_id, idem_key, claim_type, "rejected", None,
         json.dumps({"code": exc.code, "message": exc.message,
                     "status_code": exc.status_code, "details": exc.details},
                    ensure_ascii=False),
         received_utc.isoformat(), created_utc.isoformat()),
    )


def _persist_conflict_idempotency(conn, *, order_id, claim_type, idem_key, received_utc,
                                  created_utc, application_id, exc: AppError) -> None:
    """并发落败的键也登记（outcome=conflict）：同键重试回放同一 409，结果幂等。"""
    conn.execute(
        """INSERT INTO claim_idempotency(order_id, idempotency_key, claim_type, outcome,
                                         application_id, rejection_json, received_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (order_id, idem_key, claim_type, "conflict", application_id,
         json.dumps({"code": exc.code, "message": exc.message,
                     "status_code": exc.status_code, "details": exc.details},
                    ensure_ascii=False),
         received_utc.isoformat(), created_utc.isoformat()),
    )


def _persist_accepted(conn, *, voucher: dict, received_utc, created_utc) -> None:
    conn.execute(
        """INSERT INTO claim_applications(application_id, order_id, claim_type,
                                          idempotency_key, received_at, accepted_at,
                                          determination_code, generation_no, policy_version,
                                          deadline_local, deadline_utc, voucher_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (voucher["application_id"], voucher["order_id"], voucher["claim_type"],
         voucher["idempotency_key"], voucher["received_at_utc"], voucher["accepted_at_utc"],
         voucher["determination_code"], voucher["generation_no"], voucher["policy_version"],
         voucher["deadline_local"], voucher["deadline_utc"],
         json.dumps(voucher, ensure_ascii=False), created_utc.isoformat()),
    )
    conn.execute(
        """INSERT INTO claim_idempotency(order_id, idempotency_key, claim_type, outcome,
                                         application_id, rejection_json, received_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (voucher["order_id"], voucher["idempotency_key"], voucher["claim_type"], "accepted",
         voucher["application_id"], None,
         received_utc.isoformat(), created_utc.isoformat()),
    )


def _conflict_after_race(conn, *, order_id, claim_type, idem_key, received_utc):
    """唯一约束撞车后的二次判定：把 IntegrityError 翻译成明确的 409 业务错误。

    落败键同样写入幂等表（outcome=conflict），重试回放同一 409。
    """
    conn.rollback()
    created_utc = utc_now()
    same_key = conn.execute(
        "SELECT claim_type, outcome, application_id FROM claim_idempotency "
        "WHERE order_id = ? AND idempotency_key = ?",
        (order_id, idem_key),
    ).fetchone()
    if same_key is not None:
        if same_key["claim_type"] != claim_type:
            exc = AppError(
                "IDEMPOTENCY_KEY_CONFLICT",
                f"幂等键 {idem_key} 已用于理赔类型 {same_key['claim_type']}，"
                f"不能再用于 {claim_type}；请更换幂等键",
                409,
                {"order_id": order_id, "idempotency_key": idem_key,
                 "existing_claim_type": same_key["claim_type"],
                 "requested_claim_type": claim_type},
            )
        elif same_key["outcome"] == "accepted":
            row = conn.execute(
                "SELECT voucher_json FROM claim_applications WHERE application_id = ?",
                (same_key["application_id"],),
            ).fetchone()
            exc = AppError(
                "CLAIM_ALREADY_ACCEPTED",
                f"该订单的 {claim_type} 理赔已受理（并发请求胜出），同订单同类型至多一张凭证",
                409,
                {"order_id": order_id, "claim_type": claim_type,
                 "existing_application_id": same_key["application_id"],
                 "voucher": json.loads(row["voucher_json"]) if row else None},
            )
        else:
            # 键此前已落为 rejected/conflict：回放原错误
            return _replay_rejection(conn.execute(
                "SELECT rejection_json FROM claim_idempotency "
                "WHERE order_id = ? AND idempotency_key = ?",
                (order_id, idem_key),
            ).fetchone())
        conn.commit()
        return exc

    existing = conn.execute(
        "SELECT application_id FROM claim_applications WHERE order_id = ? AND claim_type = ?",
        (order_id, claim_type),
    ).fetchone()
    exc = AppError(
        "CLAIM_ALREADY_ACCEPTED",
        f"订单 {order_id} 的 {claim_type} 理赔已存在受理凭证，同订单同类型至多一张",
        409,
        {"order_id": order_id, "claim_type": claim_type,
         "existing_application_id": existing["application_id"] if existing else None},
    )
    if existing is not None:
        _persist_conflict_idempotency(
            conn, order_id=order_id, claim_type=claim_type, idem_key=idem_key,
            received_utc=received_utc, created_utc=created_utc,
            application_id=existing["application_id"], exc=exc,
        )
        conn.commit()
    return exc


# ------------------------------------------------------------ 受理

def submit_claim(order_id: str, req: ClaimApplicationRequest,
                 idem_key: str) -> tuple[dict, bool]:
    """受理一张理赔申请。返回 (凭证, 是否幂等重放)；拒绝抛 AppError（拒绝同样幂等）。"""
    if not idem_key or not idem_key.strip():
        raise AppError(
            "INVALID_REQUEST",
            "缺少幂等键：请在请求体 idempotency_key 或 Idempotency-Key 请求头中提供",
            422,
        )
    claim_type = req.claim_type
    received_utc = parse_dt(req.received_at) if req.received_at else utc_now()
    created_utc = utc_now()

    with db.tx() as conn:
        # 1) 幂等命中：同订单同键，返回原凭证 / 重放原拒绝；换类型 => 冲突
        seen = conn.execute(
            "SELECT claim_type, outcome, application_id FROM claim_idempotency "
            "WHERE order_id = ? AND idempotency_key = ?",
            (order_id, idem_key),
        ).fetchone()
        if seen is not None:
            if seen["claim_type"] != claim_type:
                raise AppError(
                    "IDEMPOTENCY_KEY_CONFLICT",
                    f"幂等键 {idem_key} 已用于理赔类型 {seen['claim_type']}，"
                    f"不能再用于 {claim_type}；请更换幂等键",
                    409,
                    {"order_id": order_id, "idempotency_key": idem_key,
                     "existing_claim_type": seen["claim_type"],
                     "requested_claim_type": claim_type},
                )
            if seen["outcome"] == "accepted":
                row = conn.execute(
                    "SELECT voucher_json FROM claim_applications WHERE application_id = ?",
                    (seen["application_id"],),
                ).fetchone()
                voucher = json.loads(row["voucher_json"])
                voucher["idempotent_replay"] = True
                return voucher, True
            # rejected / conflict：回放首次的 422 业务拒绝或 409 冲突，错误体一致
            rej = conn.execute(
                "SELECT rejection_json FROM claim_idempotency "
                "WHERE order_id = ? AND idempotency_key = ?",
                (order_id, idem_key),
            ).fetchone()
            raise _replay_rejection(rej)

        # 2) 同订单同类型至多一张凭证（不同键也拒绝；键仍留痕，保证该键重试幂等）
        existing = conn.execute(
            "SELECT application_id FROM claim_applications WHERE order_id = ? AND claim_type = ?",
            (order_id, claim_type),
        ).fetchone()
        if existing is not None:
            conflict = AppError(
                "CLAIM_ALREADY_ACCEPTED",
                f"订单 {order_id} 的 {claim_type} 理赔已存在受理凭证，"
                "同订单同类型至多一张",
                409,
                {"order_id": order_id, "claim_type": claim_type,
                 "existing_application_id": existing["application_id"]},
            )
            _persist_conflict_idempotency(
                conn, order_id=order_id, claim_type=claim_type, idem_key=idem_key,
                received_utc=received_utc, created_utc=created_utc,
                application_id=existing["application_id"], exc=conflict,
            )
            conn.commit()
            raise conflict

        # 3) 以接收时间实时核验当前代（已归档也重算，不读归档快照）
        order, policy = _load_eval_inputs(order_id)
        gen = _latest_gen(conn, order_id)
        archived = bool(gen["archived"])
        rows = _events_until_gen(conn, order_id, gen["id"])
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
        determination = evaluate(order, rows, policy, as_of=received_utc,
                                 generation_info=gen_info)
        claim = next(c for c in determination["claims"] if c["claim_type"] == claim_type)

        if claim["status"] != "filable":
            exc = _rejection_payload(order=order, claim_type=claim_type, idem_key=idem_key,
                                     received_utc=received_utc, determination=determination,
                                     claim=claim, status=claim["status"])
            # 拒绝结果也幂等落库；先落库、事务提交后再抛错（抛错不回滚）
            _persist_rejection(conn, order_id=order_id, claim_type=claim_type,
                               idem_key=idem_key, received_utc=received_utc,
                               created_utc=created_utc, exc=exc)
            conn.commit()
            raise exc

        application_id = "CLM-" + uuid.uuid4().hex[:16].upper()
        voucher = _build_voucher(
            application_id=application_id, order=order, claim_type=claim_type,
            idem_key=idem_key, received_utc=received_utc, accepted_utc=created_utc,
            determination=determination, claim=claim, policy=policy, archived=archived,
        )
        try:
            _persist_accepted(conn, voucher=voucher, received_utc=received_utc,
                              created_utc=created_utc)
        except sqlite3.IntegrityError:
            # 并发/多进程下唯一约束兜底：翻译成明确的 409
            raise _conflict_after_race(
                conn, order_id=order_id, claim_type=claim_type, idem_key=idem_key)

    out = deepcopy(voucher)
    out["idempotent_replay"] = False
    return out, False


# ------------------------------------------------------------ 查询

def _row_to_voucher(row: sqlite3.Row) -> dict:
    voucher = json.loads(row["voucher_json"])
    voucher["idempotent_replay"] = False
    return voucher


def get_application(order_id: str, application_id: str) -> dict:
    row = q1(
        "SELECT voucher_json FROM claim_applications WHERE application_id = ? AND order_id = ?",
        (application_id, order_id),
    )
    if row is None:
        raise AppError(
            "CLAIM_NOT_FOUND",
            f"凭证不存在: {application_id}（订单 {order_id}）",
            404,
            {"order_id": order_id, "application_id": application_id},
        )
    return _row_to_voucher(row)


def list_applications(order_id: str,
                      claim_type: str | None = None,
                      determination_code: str | None = None) -> dict:
    from .services import require_order
    require_order(order_id)
    sql = ("SELECT application_id, claim_type, accepted_at, determination_code, voucher_json "
           "FROM claim_applications WHERE order_id = ?")
    params: list = [order_id]
    if claim_type:
        sql += " AND claim_type = ?"
        params.append(claim_type)
    if determination_code:
        sql += " AND determination_code = ?"
        params.append(determination_code)
    sql += " ORDER BY accepted_at, application_id"
    rows = db.q(sql, tuple(params))
    items = []
    for r in rows:
        voucher = json.loads(r["voucher_json"])
        voucher["idempotent_replay"] = False
        items.append(voucher)
    return {"order_id": order_id, "count": len(items), "applications": items}
