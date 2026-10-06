"""端到端调用示例（仅使用标准库 urllib）：

  python examples/call_api.py            # 默认 http://localhost:8000
  BASE_URL=http://host:port python examples/call_api.py

演示链路：
1. 创建 2025 版政策下的订单（承诺 2/10 18:00 送达）；
2. 提交"迟到签收"扫描事件 -> 查询判定（延迟可申请，截止按工作日跳过春节）；
3. 归档判定；
4. 重放同一事件 -> 不重复计入；倒序事件 -> 明确错误；
5. 无写入预演迟到的更正事件 -> 模拟新一代结果与判定差异，系统状态不变；
6. 正式提交该更正事件 -> 产生第 2 代新判定，与预演结果一致，第 1 代快照不变；
7. 查询判定历史；
8. 理赔申请受理：按服务接收时间核验并领取不可变凭证；
   同键同类型重试返回原凭证；换键重复受理 / 换类型复用键 => 409；
   截止后申请被明确拒绝且不出凭证；按凭证号 / 判定代号查询。
"""
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
ORDER_ID = "DEMO-20250205-001"
CLAIM_ORDER_ID = "DEMO-CLAIM-20250205-001"


def call(method: str, path: str, payload=None, params=None, headers=None):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            print(f"[{method} {path}] -> {resp.status}")
            return body
    except urllib.error.HTTPError as e:
        print(f"[{method} {path}] -> {e.code} 错误：")
        print(json.dumps(json.loads(e.read().decode()), ensure_ascii=False, indent=2))
        return None


def show(title, body, keys=("overall_status", "eligible_to_file", "generation_no",
                            "earliest_deadline_local", "missing_evidence")):
    print(f"\n===== {title} =====")
    if body:
        print(json.dumps({k: body.get(k) for k in keys if k in body},
                         ensure_ascii=False, indent=2))


def main():
    print(f"目标服务：{BASE}")

    # 幂等：演示订单已存在则删除无接口，直接换 ID
    order_payload = {
        "order_id": ORDER_ID,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    }
    r = call("POST", "/api/v1/orders", order_payload)
    if r is None:
        print("订单创建失败，请确认服务已启动且已初始化（docker compose up 会自动初始化）")
        return
    print("适用政策版本：", r["created_policy"])

    signed_event = {
        "event_id": "EV-SIGNED-01",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",    # 扫描发生时间（晚于承诺时限）
        "arrived_at": "2025-02-11T10:05:00+08:00",    # 到达服务时间（顺序以此为准）
    }
    call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)

    det = call("GET", f"/api/v1/orders/{ORDER_ID}/determination",
               params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("实时判定：延迟送达，10 个工作日截止（2/11 起算，跳过春节后为 2/25 23:59:59）", det)
    if det:
        print("\n延迟类判定依据：")
        for line in det["claims"][0]["basis"]:
            print("  -", line)

    archived = call("POST", f"/api/v1/orders/{ORDER_ID}/determination/archive",
                    params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("归档第 1 代判定", archived)

    dup = call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)
    show("重放同一事件：accepted=0、duplicates 记录", dup,
         keys=("accepted", "duplicates", "generation_no"))

    bad = {"events": [
        {"event_id": "EV-LATE", "event_type": "lost_confirmed",
         "event_time": "2025-02-15T09:00:00+08:00",
         "arrived_at": "2025-02-09T08:00:00+08:00"},  # 早于已接受的 2/11
    ]}
    print("\n===== 倒序事件提交：期望 422 EVENT_ORDER_INVALID =====")
    call("POST", f"/api/v1/orders/{ORDER_ID}/events/batch", bad)

    correction = {
        "event_id": "EV-CORR-01",
        "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",    # 修正后的签收时间（未迟到）
        "arrived_at": "2025-02-21T08:00:00+08:00",    # 晚于归档 -> 迟到更正
        "correction_action": "amend",
        "target_event_id": "EV-SIGNED-01",
        "note": "网点补传准确签收时间",
    }

    # 无写入预演：正式提交前，先模拟这批更正事件的影响（不落库、不开新代）
    preview = call("POST", f"/api/v1/orders/{ORDER_ID}/events/preview",
                   {"as_of": "2025-02-21T12:00:00+08:00", "events": [correction]})
    print("\n===== 无写入预演：模拟新一代结果与判定差异 =====")
    if preview:
        sim = preview["simulation"]
        print(f"  校验通过={preview['valid']}，首个失败位置={preview['first_failure_index']}")
        print(f"  模拟代=第 {sim['generation_no']} 代（would_reopen={sim['would_reopen']}，"
              f"归档快照不受影响）")
        for c in preview["diff"]["claims"]:
            if c["changed"]:
                print(f"  {c['claim_type']}: {c['status_before']} -> {c['status_after']}")
        print("  判定依据新增：")
        for line in preview["diff"]["basis_added"]:
            print("   +", line)

    reopened = call("POST", f"/api/v1/orders/{ORDER_ID}/events", correction)
    show("正式提交迟到更正：自动开启第 2 代", reopened,
         keys=("accepted", "reopened", "generation_no", "note"))

    det2 = call("GET", f"/api/v1/orders/{ORDER_ID}/determination")
    show("第 2 代判定：延迟不再成立（not_applicable），与预演一致，旧归档未被改写", det2)

    hist = call("GET", f"/api/v1/orders/{ORDER_ID}/determinations")
    print("\n===== 判定历史 =====")
    if hist:
        for g in hist["generations"]:
            status = g.get("overall_status", "（未归档）")
            print(f"  第 {g['generation_no']} 代 | archived={g['archived']} | "
                  f"状态={status} | supersedes={g['supersedes_generation']}")

    demo_claim_acceptance()

    print("\n示例完成。交互文档：" + BASE + "/docs")


def demo_claim_acceptance():
    """理赔申请受理链路（独立订单，避免与上面更正链路互相影响）。"""
    print("\n\n########## 理赔申请受理（不可变凭证） ##########")
    payload = {
        "order_id": CLAIM_ORDER_ID,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    }
    r = call("POST", "/api/v1/orders", payload)
    if r is None:
        print("受理演示订单创建失败，跳过后续步骤")
        return
    call("POST", f"/api/v1/orders/{CLAIM_ORDER_ID}/events", {
        "event_id": "EV-SIGNED-C1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00",
    })

    claims_path = f"/api/v1/orders/{CLAIM_ORDER_ID}/claims"

    print("\n===== 1) 时限内（接收时间 2/20）提交延迟理赔：201 出凭证 =====")
    v = call("POST", claims_path, {
        "claim_type": "delay",
        "idempotency_key": "IDEMP-DELAY-001",
        "received_at": "2025-02-20T12:00:00+08:00",   # 服务实际接收时间（核验基准）
        "note": "客户网点柜面提交",
    })
    if not v:
        return
    print(json.dumps({
        "application_id": v["application_id"],
        "accepted_at_utc": v["accepted_at_utc"],
        "determination_code": v["determination_code"],
        "policy_version": v["policy_version"],
        "deadline_local": v["deadline_local"],
        "generation_archived_at_receipt": v["generation_archived_at_receipt"],
    }, ensure_ascii=False, indent=2))
    application_id = v["application_id"]
    print("该类型当时判定依据：")
    for line in v["determination_basis"]["claim_basis"]:
        print("  -", line)

    print("\n===== 2) 同订单同键同类型重试：返回原凭证（200, idempotent_replay=true） =====")
    v2 = call("POST", claims_path, {
        "claim_type": "delay", "idempotency_key": "IDEMP-DELAY-001",
        "received_at": "2025-03-01T12:00:00+08:00"})   # 即使给更晚的接收时间也不改判
    if v2:
        print(f"  application_id 相同={v2['application_id'] == application_id}, "
              f"idempotent_replay={v2['idempotent_replay']}")

    print("\n===== 3) 同一幂等键改报破损：409 IDEMPOTENCY_KEY_CONFLICT =====")
    call("POST", claims_path, {
        "claim_type": "damage", "idempotency_key": "IDEMP-DELAY-001",
        "received_at": "2025-02-20T12:00:00+08:00"})

    print("\n===== 4) 换一个键重复申请同类型：409 CLAIM_ALREADY_ACCEPTED =====")
    call("POST", claims_path, {
        "claim_type": "delay", "idempotency_key": "IDEMP-DELAY-002",
        "received_at": "2025-02-20T12:00:00+08:00"})

    print("\n===== 5) 触发事件尚未出现（2/8 破损类 pending）：422 CLAIM_PENDING，无凭证 =====")
    call("POST", claims_path, {
        "claim_type": "damage", "idempotency_key": "IDEMP-DMG-001",
        "received_at": "2025-02-08T12:00:00+08:00"})

    print("\n===== 6) 截止后（2/26）才申请丢失类（pending）：422 CLAIM_PENDING =====")
    call("POST", claims_path, {
        "claim_type": "loss", "idempotency_key": "IDEMP-LOSS-001",
        "received_at": "2025-02-26T08:00:00+08:00"})

    print("\n===== 7) 幂等键也可用 Idempotency-Key 请求头提供 =====")
    call("POST", claims_path,
         {"claim_type": "loss", "received_at": "2025-02-12T09:00:00+08:00"},
         headers={"Idempotency-Key": "IDEMP-LOSS-HDR"})

    print("\n===== 8) 查询凭证及其判定代号/判定依据 =====")
    got = call("GET", f"{claims_path}/{application_id}")
    if got:
        print(f"  {got['application_id']} | {got['determination_code']} | "
              f"截止 {got['deadline_local']}")

    listed = call("GET", claims_path, params={"determination_code": "DELAY_FILABLE"})
    if listed:
        print(f"  按代号 DELAY_FILABLE 过滤：{listed['count']} 张凭证")


if __name__ == "__main__":
    main()
