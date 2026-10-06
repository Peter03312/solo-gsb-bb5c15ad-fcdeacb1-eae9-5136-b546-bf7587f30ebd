"""理赔申请受理与不可变凭证端到端测试。

覆盖：
- 受理：以接收时间核验当前代有效事件/锁定政策/当地截止，filable 才出凭证；
- 可区分的业务拒绝（pending / not_filable / not_applicable / indeterminate），不出凭证；
- 已归档代也按接收时间重算，不采用归档快照；归档快照本身不变；
- 幂等：同键同类型返回原凭证；换类型 409；不同键并发同类型不双重受理；拒绝也幂等；
- 凭证不可变（后续更正/新代不影响）；可按类型/判定代号查询。
"""
import threading

from tests.conftest import make_order

CLAIMS = "/api/v1/orders/O1/claims"


def _signed(event_id="s1", event_time="2025-02-11T10:00:00+08:00",
            arrived_at="2025-02-11T10:05:00+08:00"):
    return {"event_id": event_id, "event_type": "signed",
            "event_time": event_time, "arrived_at": arrived_at}


def _apply(client, key="K1", claim_type="delay",
           received_at="2025-02-20T12:00:00+08:00", order_id="O1"):
    return client.post(f"/api/v1/orders/{order_id}/claims",
                       json={"claim_type": claim_type, "idempotency_key": key,
                             "received_at": received_at})


# ------------------------------------------------------------ 受理成功

def test_filable_claim_accepted_with_immutable_voucher(client):
    make_order(client)  # v2：延迟 10 个工作日；2/11 签收迟到 -> 截止 2/25 23:59:59
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = _apply(client)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["application_id"].startswith("CLM-")
    assert v["claim_type"] == "delay"
    assert v["status"] == "accepted"
    assert v["idempotency_key"] == "K1"
    assert v["idempotent_replay"] is False
    # 受理时间 / 接收时间
    assert v["received_at_local"].startswith("2025-02-20T12:00:00")
    assert v["accepted_at_utc"]
    # 判定代号 / 适用政策 / 截止时间
    assert v["determination_code"] == "DELAY_FILABLE"
    assert v["policy_version"] == "v2"
    assert v["applied_policy"]["version"] == "v2"
    assert v["applied_policy"]["locked_at_order_creation"] is True
    assert v["applied_policy"]["window"]["days"] == 10
    assert v["deadline_local"].startswith("2025-02-25T23:59:59")
    assert v["deadline_utc"]
    # 当时判定依据随凭证固化
    assert v["determination_basis"]["claim_status"] == "filable"
    assert v["determination_basis"]["top_level"]
    assert any("构成延迟送达" in b for b in v["determination_basis"]["claim_basis"])
    assert any("接收时间" in b for b in v["basis"])
    assert any("不可变" in b for b in v["basis"])
    assert v["generation_no"] == 1


def test_default_received_at_uses_service_time_when_omitted(client):
    # 2025 订单 + 迟到签收：按当前（2026 年）服务时间必然已过截止 -> 拒绝
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = client.post(CLAIMS, json={"claim_type": "delay", "idempotency_key": "K1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CLAIM_NOT_FILABLE"


def test_idempotency_key_accepted_via_header(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = client.post(CLAIMS, json={"claim_type": "delay",
                                  "received_at": "2025-02-20T12:00:00+08:00"},
                    headers={"Idempotency-Key": "HDR-1"})
    assert r.status_code == 201
    assert r.json()["idempotency_key"] == "HDR-1"


def test_missing_idempotency_key_rejected(client):
    make_order(client)
    r = client.post(CLAIMS, json={"claim_type": "delay"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_unknown_order_404(client):
    r = client.post("/api/v1/orders/NOPE/claims",
                    json={"claim_type": "delay", "idempotency_key": "K1"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_invalid_claim_type_422(client):
    make_order(client)
    r = client.post(CLAIMS, json={"claim_type": "theft", "idempotency_key": "K1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_naive_received_at_422(client):
    make_order(client)
    r = client.post(CLAIMS, json={"claim_type": "delay", "idempotency_key": "K1",
                                  "received_at": "2025-02-20T12:00:00"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# ------------------------------------------------------------ 可区分的业务拒绝

def test_rejected_when_pending_no_trigger(client):
    make_order(client)  # 无任何扫描：delay 在承诺时限前为 pending
    r = _apply(client, received_at="2025-02-08T12:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_PENDING"
    assert err["details"]["determination_code"] == "DELAY_PENDING"
    assert err["details"]["policy_version"] == "v2"
    assert err["details"]["basis"]
    # 不出凭证
    assert client.get(CLAIMS).json()["count"] == 0


def test_rejected_when_after_deadline(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    r = _apply(client, received_at="2025-02-26T08:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_NOT_FILABLE"
    assert err["details"]["determination_code"] == "DELAY_NOT_FILABLE"
    assert err["details"]["deadline_local"].startswith("2025-02-25T23:59:59")
    assert any("截止" in b for b in err["details"]["basis"])


def test_rejected_when_not_applicable(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed(
        event_time="2025-02-10T09:00:00+08:00",
        arrived_at="2025-02-10T09:05:00+08:00"))  # 准时签收
    r = _apply(client, received_at="2025-02-10T12:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_NOT_APPLICABLE"
    assert err["details"]["determination_code"] == "DELAY_NOT_APPLICABLE"
    assert err["details"]["deadline_local"] is None


def test_rejected_when_indeterminate(client):
    make_order(client, promise_by=None)  # 无承诺时限 => delay indeterminate
    client.post("/api/v1/orders/O1/events", json=_signed(
        event_time="2025-02-20T10:00:00+08:00",
        arrived_at="2025-02-20T10:05:00+08:00"))
    r = _apply(client, received_at="2025-02-20T12:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "CLAIM_INDETERMINATE"
    assert err["details"]["determination_code"] == "DELAY_INDETERMINATE"


# ------------------------------------------------------------ 已归档代重算

def test_archived_generation_is_rechecked_not_snapshot(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    # 2/12 归档：快照内 delay 为 filable
    snap = client.post("/api/v1/orders/O1/determination/archive",
                       params={"as_of": "2025-02-12T10:00:00+08:00"}).json()
    assert snap["claims"][0]["status"] == "filable"

    # 截止后（2/26）才提交申请：必须按接收时间重算 -> 拒绝，而不是用 filable 快照受理
    r = _apply(client, received_at="2025-02-26T08:00:00+08:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CLAIM_NOT_FILABLE"

    # 时限内提交则受理，并明确标注"已归档代按接收时间重新核验"
    r2 = _apply(client, key="K2", received_at="2025-02-20T12:00:00+08:00")
    assert r2.status_code == 201
    v = r2.json()
    assert v["generation_archived_at_receipt"] is True
    assert any("重新核验" in b for b in v["basis"])
    # 归档快照原样保留
    hist = client.get("/api/v1/orders/O1/determinations").json()
    assert hist["generations"][0]["snapshot"]["claims"][0]["status"] == "filable"


# ------------------------------------------------------------ 幂等

def test_same_key_same_type_retry_returns_original_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    v1 = _apply(client).json()
    # 即便重试时给的接收时间更晚（已过截止），仍返回原凭证——判定以首次接收为准
    r2 = _apply(client, received_at="2025-02-26T08:00:00+08:00")
    assert r2.status_code == 200
    v2 = r2.json()
    assert v2["idempotent_replay"] is True
    assert v2["application_id"] == v1["application_id"]
    assert v2["received_at_utc"] == v1["received_at_utc"]
    assert v2["basis"] == v1["basis"]
    assert v2["determination_code"] == v1["determination_code"]


def test_same_key_different_type_conflicts(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    assert _apply(client, claim_type="delay").status_code == 201
    r = _apply(client, claim_type="damage",
               received_at="2025-02-20T12:00:00+08:00")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert err["details"]["existing_claim_type"] == "delay"
    assert err["details"]["requested_claim_type"] == "damage"


def test_same_type_different_key_second_request_conflicts(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    assert _apply(client, key="K1").status_code == 201
    r = _apply(client, key="K2")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "CLAIM_ALREADY_ACCEPTED"
    assert err["details"]["existing_application_id"].startswith("CLM-")
    # 仍只有一张凭证
    assert client.get(CLAIMS).json()["count"] == 1


def test_different_types_each_get_one_voucher(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-11T11:10:00+08:00"})
    rd = _apply(client, key="D1", claim_type="delay")
    rg = _apply(client, key="G1", claim_type="damage",
                received_at="2025-02-11T15:00:00+08:00")
    assert rd.status_code == 201 and rg.status_code == 201
    body = client.get(CLAIMS).json()
    assert body["count"] == 2
    codes = {a["determination_code"] for a in body["applications"]}
    assert codes == {"DELAY_FILABLE", "DAMAGE_FILABLE"}


def test_rejection_is_also_idempotent(client):
    make_order(client)
    r1 = _apply(client, received_at="2025-02-08T12:00:00+08:00")
    assert r1.status_code == 422 and r1.json()["error"]["code"] == "CLAIM_PENDING"
    # 同键同类型重试：返回同一个可区分拒绝；换一个已可受理的接收时间也不改变首次判定
    r2 = _apply(client, received_at="2025-02-20T12:00:00+08:00")
    assert r2.status_code == 422
    e1, e2 = r1.json()["error"], r2.json()["error"]
    assert e2["code"] == e1["code"] == "CLAIM_PENDING"
    assert e2["details"]["determination_code"] == "DELAY_PENDING"


def test_rejected_key_reuse_by_different_type_conflicts(client):
    make_order(client)
    r1 = _apply(client, claim_type="delay",
                received_at="2025-02-08T12:00:00+08:00")
    assert r1.status_code == 422
    r2 = _apply(client, claim_type="damage",
                received_at="2025-02-08T12:00:00+08:00")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_concurrent_different_keys_do_not_double_accept(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    from app import db
    from app.schemas import ClaimApplicationRequest
    from app.claims import submit_claim

    barrier = threading.Barrier(5)
    results = []

    def worker(key):
        barrier.wait()
        req = ClaimApplicationRequest(
            claim_type="delay", idempotency_key=key,
            received_at="2025-02-20T12:00:00+08:00")
        try:
            voucher, replay = submit_claim("O1", req, key)
            results.append(("accepted", voucher["application_id"]))
        except Exception as exc:  # noqa: BLE001
            results.append((getattr(exc, "code", type(exc).__name__), None))

    threads = [threading.Thread(target=worker, args=(f"K{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [r for r in results if r[0] == "accepted"]
    conflicts = [r for r in results if r[0] == "CLAIM_ALREADY_ACCEPTED"]
    assert len(accepted) == 1
    assert len(conflicts) == 4
    rows = db.q("SELECT COUNT(*) AS n FROM claim_applications WHERE order_id = 'O1'")
    assert rows[0]["n"] == 1
    rows = db.q("SELECT idempotency_key FROM claim_idempotency "
                "WHERE order_id = 'O1' AND outcome = 'conflict'")
    loser_keys = [r["idempotency_key"] for r in rows]
    assert len(loser_keys) == 4
    rows = db.q("SELECT COUNT(*) AS n FROM claim_idempotency WHERE order_id = 'O1'")
    assert rows[0]["n"] == 5  # 每次尝试都留痕；凭证只有一张

    # 落败键重试：回放同一 409（而非再次首次受理），结果稳定幂等
    loser = loser_keys[0]
    retry = _apply(client, key=loser, claim_type="delay")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "CLAIM_ALREADY_ACCEPTED"
    assert retry.json()["error"]["details"]["existing_application_id"] == accepted[0][1]


# ------------------------------------------------------------ 凭证不可变与查询

def test_voucher_immutable_after_later_correction_new_generation(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    v1 = _apply(client).json()

    # 受理后提交迟到更正：签收被修正到承诺时限前 -> 第 2 代 delay 不成立
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-20T12:00:00+08:00"})
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-21T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"})

    live = client.get("/api/v1/orders/O1/determination").json()
    assert live["generation_no"] == 2
    assert live["claims"][0]["status"] == "not_applicable"

    # 凭证仍原样可查，判定代号/依据不变
    got = client.get(f"{CLAIMS}/{v1['application_id']}").json()
    assert got["application_id"] == v1["application_id"]
    assert got["determination_code"] == "DELAY_FILABLE"
    assert got["basis"] == v1["basis"]
    assert got["generation_no"] == 1
    assert got["determination_basis"]["claim_basis"] == \
        v1["determination_basis"]["claim_basis"]


def test_list_filter_by_type_and_determination_code(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=_signed())
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-11T11:10:00+08:00"})
    _apply(client, key="D1", claim_type="delay")
    _apply(client, key="G1", claim_type="damage",
           received_at="2025-02-11T15:00:00+08:00")

    delay = client.get(CLAIMS, params={"claim_type": "delay"}).json()
    assert delay["count"] == 1 and delay["applications"][0]["claim_type"] == "delay"

    by_code = client.get(CLAIMS, params={"determination_code": "DAMAGE_FILABLE"}).json()
    assert by_code["count"] == 1
    assert by_code["applications"][0]["determination_code"] == "DAMAGE_FILABLE"


def test_get_unknown_voucher_404(client):
    make_order(client)
    r = client.get(f"{CLAIMS}/CLM-NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CLAIM_NOT_FOUND"
