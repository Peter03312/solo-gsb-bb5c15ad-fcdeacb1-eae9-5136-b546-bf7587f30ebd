# 快递理赔时限判定服务

按**订单创建时生效的政策版本**判定快递理赔申请资格的 HTTP 服务。输入订单、承诺时限、时区、节假日表与按时间到达的扫描事件，输出：

- 当前是否可申请（`eligible_to_file` / `overall_status`）；
- 各理赔类型的**截止时间**（当地时间 + UTC）；
- **缺失证据**清单；
- 逐条可审计的**判定依据**（含适用政策版本、起算事件、跳节假日说明、更正记录）；
- 客户正式提交理赔申请时的**受理判定与不可变凭证**（按服务接收时间核验、幂等、可查询）。

## 判定规则

### 政策版本锁定

- 政策以 `valid_from / valid_to`（UTC，半开区间）入库；
- 下单时选取 `valid_from <= 订单创建时间 < valid_to` 的最新版本，版本号随订单持久化；
- 之后发布新政不影响既有订单（切换政策版本无需改代码，只更新数据）。

内置两个演示版本（见 `app/seed_data.py`）：

| 版本 | 生效区间（UTC） | 延迟送达 | 货物破损 | 货物丢失 |
|---|---|---|---|---|
| v1 | 2023-01-01 ～ 2025-01-01 | 7 个工作日 | 5 个自然日 | 30 个自然日 |
| v2 | 2025-01-01 起 | 10 个工作日 | 3 个工作日 | 15 个工作日 |

### 工作日与节假日

- 截止日按订单时区计算：`触发日次日起算 N 天，截止落在第 N 天的 23:59:59（当地时间）`；
- `business`（工作日）口径跳过周六日**和**节假日表中的 `holiday`；
- 节假日表中的 `workday`（调休补班日，即使是周末）**计为工作日**；
- 内置中国国务院办公厅 2024、2025 年放假/调休安排（72 个特殊日期）。

例：2025-02-11（周二）签收迟到，10 个工作日的截止为 **2025-02-25 23:59:59（Asia/Shanghai）**——其间春节假期（1/28–2/4 在起算前、无影响；本次推算仅跳过普通周末）。2025-10-01 起算则跳过国庆+中秋黄金周，截止 **2025-10-21**。

### 理赔类型与状态

三类理赔独立判定：

- `delay` 延迟送达：签收晚于承诺时限、或承诺时限已过仍无签收 → 构成；未到承诺时限无签收 → `pending`；准时签收 → `not_applicable`；订单未给承诺时限 → `indeterminate`；
- `damage` 货物破损：出现 `damage_reported`（破损申报）扫描即触发；
- `loss` 货物丢失：出现 `lost_confirmed`（丢件确认）扫描即触发。

每个类型状态：`filable`（可申请）/ `pending`（等待触发事件）/ `not_filable`（已过截止）/ `not_applicable`（不成立）/ `indeterminate`（证据不足不可判定）。顶层 `overall_status`：任一可申请即 `filable`，否则有 `pending/indeterminate` 即 `pending`，全部 `not_applicable` 为 `closed`。

### 事件流规则（重点）

1. **按到达时间顺序入库**：以 `arrived_at`（服务接收时间）为准，必须相对已接受流单调不减。倒序提交返回 `422 EVENT_ORDER_INVALID` 并标明出错下标，**整批回滚**。
2. **重放不重复计入**：`event_id` 在同一订单内唯一；重复提交进入 `duplicates`，不插入、不参与判定。
3. **时间字段必须带时区偏移**（如 `2025-02-11T10:00:00+08:00`），字段缺失/时间 naive 一律 `422 INVALID_REQUEST`。
4. **迟到更正不改写归档**：判定可 `POST .../archive` 归档（生成不可变 JSON 快照）。归档后到达的任何事件（含更正）会**开启新一代（generation）判定**，旧代快照原样保留；新结果通过 `supersedes_generation` 指向旧代。
5. **更正事件三种动作**（`is_correction` 语义由 `correction_action` 表达）：
   - `amend`：修正目标基础事件的发生时间（须带 `target_event_id`，类型须一致）；
   - `cancel`：撤销目标事件（如丢件确认被推翻）；
   - `insert`：补录一条此前漏掉的扫描（允许类型：signed / damage_reported / lost_confirmed）。
   更正只影响"物化"后的判定视图，原始事件行永不修改或删除。
6. **无写入预演**：`POST .../events/preview` 在当前最新代事件视图上按同一套规则模拟整批处理，返回逐项校验结果、首个失败位置、模拟后的各理赔状态/截止时间/缺失证据与判定依据差异；任一事件违反约束即整批预演失败（错误码与正式摄取一致），全程不写事件、不开新代、不改归档快照。已归档订单的预演明确按**新一代**（`gen_no+1`）结果计算。

## 理赔申请受理与不可变凭证（v1.1 新增）

判定接口回答"现在能不能申请"；**受理接口**回答"客户此刻正式提交的这张申请，收不收、出不出凭证"。

### 核验口径

客户按**订单 + 理赔类型**提交申请（`POST /api/v1/orders/{id}/claims`），服务以**实际接收时间** `received_at`（带时区偏移；缺省取服务当前 UTC 时间）为唯一核验基准，重新核验：

1. 该订单**当前代**经更正物化后的有效扫描事件；
2. **下单时锁定的政策版本**（不使用任何后发新政）；
3. 该类型在订单时区/节假日表下的**当地截止时刻**（同口径比较）。

即使当前代**已归档**，也按接收时间**重新核验**，绝不把较早的归档快照当成实时资格；归档快照本身原样保留。凭证内 `generation_archived_at_receipt=true` 并在判定依据中显式说明这一点。

### 受理结果

- 仅当该类型状态为 **`filable`** 才受理（HTTP 201），返回不可变凭证：申请编号 `application_id`（`CLM-xxxxxxxx`）、受理时间 `accepted_at`、**判定代号**（`DELAY_FILABLE` / `DAMAGE_FILABLE` / `LOSS_FILABLE`）、适用政策 `applied_policy`、当地截止时间 `deadline_local`/`deadline_utc`、以及 `basis` 与 `determination_basis`（当时逐条判定依据与证据快照）。
- 其余状态一律拒绝（HTTP 422），返回**可区分的业务错误码**，**不生成凭证**：

  | 类型状态 | 错误码 | 判定代号（details.determination_code） |
  |---|---|---|
  | `pending` | `CLAIM_PENDING` | `DELAY_PENDING` 等 |
  | `not_filable`（已过截止） | `CLAIM_NOT_FILABLE` | `DELAY_NOT_FILABLE` 等 |
  | `not_applicable`（事由不成立） | `CLAIM_NOT_APPLICABLE` | `DELAY_NOT_APPLICABLE` 等 |
  | `indeterminate`（证据不足） | `CLAIM_INDETERMINATE` | `DELAY_INDETERMINATE` 等 |

  错误体沿用统一约定，`error.details` 内含判定代号、政策版本、截止时间与该类型判定依据。

### 幂等与并发

幂等键取请求体 `idempotency_key` 或 `Idempotency-Key` 请求头（二选一，必填）：

- **同订单 + 同键 + 同类型重试**：返回原凭证（HTTP 200，`idempotent_replay=true`）；即使重试时携带更晚的 `received_at`，判定仍以首次接收为准。**业务拒绝同样幂等**——同键重放返回与首次一致的拒绝体；
- **同订单 + 同键 + 换类型**：`409 IDEMPOTENCY_KEY_CONFLICT`（不受理、不出凭证）；
- **同订单 + 同类型至多一张凭证**：换不同键再次提交 => `409 CLAIM_ALREADY_ACCEPTED`，details 给出已存在的凭证号；并发不同键同时提交由数据库唯一约束 + 串行写事务兜底，**绝不双重受理**（落败键也留痕，其重试回放同一 409）。

### 不可变与查询

凭证及其判定依据**一经受理不可变**：之后的迟到更正、新一代判定只影响新的实时判定，已出凭证内容不变。

- `GET .../claims`：列出订单凭证，可按 `claim_type`、`determination_code`（如 `DELAY_FILABLE`）过滤；
- `GET .../claims/{application_id}`：取单张凭证及其所依据的判定代号与判定依据；
- 凭证不存在 => `404 CLAIM_NOT_FOUND`。

## 目录结构

```
app/
  config.py        # 环境变量配置（数据目录、默认截止钟点）
  constants.py     # 事件/理赔类型与中文标签
  db.py            # SQLite 连接、建表、串行化写事务
  engine.py        # 纯函数核心：政策选取/工作日推算/事件物化/判定
  errors.py        # 业务错误码（含理赔受理拒绝码与冲突码）
  schemas.py       # Pydantic 入参模型与时区校验（含理赔受理请求）
  seed_data.py     # 政策版本 + CN 2024/2025 节假日种子
  services.py      # 订单、事件摄取、归档/开代、判定装配
  claims.py        # 理赔申请受理：接收时间核验/不可变凭证/幂等与并发裁决
  main.py          # FastAPI 路由
init_db.py         # 初始化入口（建表 + 种子，幂等；新表随启动自动创建）
examples/call_api.py  # 端到端调用示例（仅标准库，含受理/幂等/拒绝/查询链路）
tests/             # pytest 引擎单测 + API 端到端测试（63 个用例）
Dockerfile, docker-compose.yml
```

## 快速启动

### 方式一：Docker Compose（推荐，自动初始化）

```bash
docker compose up --build
# 服务：http://localhost:8000  交互文档：http://localhost:8000/docs
```

数据持久化在命名卷 `claim-data`（容器内 `/data`，SQLite 单文件）。

### 方式二：本地 Python（3.11+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python init_db.py                       # 建表 + 写入政策与节假日（幂等）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

重新初始化（清空重来）：停止服务后删除数据目录（默认 `./data`），再执行 `python init_db.py`。

### 运行调用示例

```bash
pip install -r requirements.txt          # 示例只用标准库，装不装均可
python examples/call_api.py              # BASE_URL 可改目标地址
```

### 运行测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## HTTP API

所有请求/响应均为 JSON。错误统一形如：

```json
{"error": {"code": "EVENT_ORDER_INVALID", "message": "...", "details": {...}}}
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/orders` | 创建订单（按创建时间锁定政策版本） |
| GET | `/api/v1/orders/{id}` | 查询订单 |
| POST | `/api/v1/orders/{id}/events` | 提交单个扫描事件 |
| POST | `/api/v1/orders/{id}/events/batch` | 批量提交（顺序错误整批回滚） |
| POST | `/api/v1/orders/{id}/events/preview` | 无写入预演：模拟整批摄取，返回校验结果与判定差异 |
| GET | `/api/v1/orders/{id}/events` | 查看已接受事件流 |
| GET | `/api/v1/orders/{id}/determination?as_of=...` | 当前判定（最新代已归档则返回快照） |
| POST | `/api/v1/orders/{id}/determination/archive?as_of=...` | 归档当前代判定 |
| GET | `/api/v1/orders/{id}/determinations` | 全部"代"的判定历史与快照 |
| POST | `/api/v1/orders/{id}/claims` | **理赔申请受理**：按接收时间核验，filable 出不可变凭证（幂等） |
| GET | `/api/v1/orders/{id}/claims?claim_type=&determination_code=` | 查询订单凭证（可按类型/判定代号过滤） |
| GET | `/api/v1/orders/{id}/claims/{application_id}` | 查询单张凭证及其判定代号/依据 |
| GET | `/api/v1/policies` | 政策版本列表 |
| GET | `/api/v1/calendars/{code}` | 节假日表 |

`as_of` 为可选判定基准时刻（带偏移的 ISO8601），缺省取服务当前 UTC 时间——便于回溯测试。

### curl 示例

**1) 创建订单**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "order_id": "O-1001",
    "created_at": "2025-02-05T09:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "calendar_code": "CN",
    "promise_by": "2025-02-10T18:00:00+08:00",
    "weight_kg": 2.5
  }'
```

**2) 提交签收扫描（晚于承诺时限）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-1",
    "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00"
  }'
```

**3) 查询判定**

```bash
curl -s "http://localhost:8000/api/v1/orders/O-1001/determination?as_of=2025-02-20T12:00:00%2B08:00"
```

响应节选：

```json
{
  "order_id": "O-1001",
  "policy_version": "v2",
  "overall_status": "filable",
  "eligible_to_file": true,
  "earliest_deadline_local": "2025-02-25T23:59:59+08:00",
  "missing_evidence": [],
  "claims": [
    {
      "claim_type": "delay",
      "status": "filable",
      "deadline_local": "2025-02-25T23:59:59+08:00",
      "missing_evidence": [],
      "basis": [
        "实际签收 2025-02-11 10:00:00 CST 晚于承诺时限 2025-02-10 18:00:00 CST，构成延迟送达",
        "时限 10 个工作日（已跳过当地节假日），截止 2025-02-25 23:59:59 CST",
        "当前时间在申请时限内，可申请"
      ]
    }
  ],
  "basis": ["适用政策版本：v2（订单于 2025-02-05T09:00:00+08:00 创建）", "..."]
}
```

**4) 归档，再提交迟到更正（产生新一代，旧结果不变）**

```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/O-1001/determination/archive?as_of=2025-02-20T12:00:00%2B08:00"

curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-CORR-1",
    "event_type": "signed",
    "event_time": "2025-02-09T09:00:00+08:00",
    "arrived_at": "2025-02-21T08:00:00+08:00",
    "correction_action": "amend",
    "target_event_id": "EV-1",
    "note": "网点补传准确签收时间"
  }'

curl -s http://localhost:8000/api/v1/orders/O-1001/determinations
```

**5) 无写入预演：正式提交前模拟更正事件的影响（不落库、不开新代）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "as_of": "2025-02-21T12:00:00+08:00",
    "events": [{
      "event_id": "EV-CORR-2",
      "event_type": "signed",
      "event_time": "2025-02-09T09:00:00+08:00",
      "arrived_at": "2025-02-21T08:00:00+08:00",
      "correction_action": "amend",
      "target_event_id": "EV-1"
    }]
  }'
```

响应节选（此时第 1 代已归档，预演明确按第 2 代计算；归档快照不变）：

```json
{
  "dry_run": true,
  "valid": true,
  "first_failure_index": null,
  "items": [{"index": 0, "event_id": "EV-CORR-2", "valid": true, "checks": [
    {"check": "shape", "passed": true}, {"check": "duplicate", "passed": true},
    {"check": "arrival_order", "passed": true}, {"check": "correction_target", "passed": true}
  ]}],
  "simulation": {
    "current_generation_archived": true,
    "would_reopen": true,
    "generation_no": 2,
    "supersedes_generation": 1,
    "determination": {"overall_status": "pending", "claims": [
      {"claim_type": "delay", "status": "not_applicable", "...": "..."}
    ]}
  },
  "diff": {
    "overall_status": {"before": "filable", "after": "pending", "changed": true},
    "claims": [{"claim_type": "delay", "status_before": "filable",
                "status_after": "not_applicable", "changed": true, "...": "..."}],
    "basis_added": ["本次判定已纳入迟到更正事件：", "..."],
    "basis_removed": []
  },
  "no_writes": "本次为无写入预演：未写入任何事件、未开启新代、归档快照未受影响"
}
```

预演失败（如到达顺序违规）返回与正式摄取相同的错误码，`error.details` 内含逐项校验结果与 `first_failure_index`，且同样零写入。

**6) 正式提交理赔申请：按服务接收时间核验，filable 出凭证**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/claims \
  -H 'Content-Type: application/json' \
  -d '{
    "claim_type": "delay",
    "idempotency_key": "crm-ticket-20250220-77",
    "received_at": "2025-02-20T12:00:00+08:00"
  }'
```

成功（201）响应节选：

```json
{
  "application_id": "CLM-A1B2C3D4E5F60718",
  "order_id": "O-1001",
  "claim_type": "delay",
  "received_at_local": "2025-02-20T12:00:00+08:00",
  "accepted_at_utc": "2025-02-20T04:00:00+00:00",
  "determination_code": "DELAY_FILABLE",
  "applied_policy": {"version": "v2", "locked_at_order_creation": true,
                     "window": {"days": 10, "day_basis": "business", "cutoff": "23:59:59"}},
  "deadline_local": "2025-02-25T23:59:59+08:00",
  "deadline_utc": "2025-02-25T15:59:59+00:00",
  "idempotent_replay": false,
  "basis": ["...", "凭证及判定依据一经受理不可变；后续更正或新一代判定不影响本凭证"],
  "determination_basis": {"claim_status": "filable", "claim_basis": ["..."], "evidence": ["..."]}
}
```

同键同类型重试得到 **200** 与原凭证（`idempotent_replay=true`）；同键改报 `damage` 得到 `409 IDEMPOTENCY_KEY_CONFLICT`；换键重复申请 `delay` 得到 `409 CLAIM_ALREADY_ACCEPTED`；接收时间晚于截止（如 `2025-02-26`）得到 `422 CLAIM_NOT_FILABLE`，错误体含判定代号 `DELAY_NOT_FILABLE`、截止时间与判定依据，且不产生凭证。

```bash
# 按凭证号查询凭证及其判定代号；按判定代号过滤订单凭证
curl -s http://localhost:8000/api/v1/orders/O-1001/claims/CLM-A1B2C3D4E5F60718
curl -s "http://localhost:8000/api/v1/orders/O-1001/claims?determination_code=DELAY_FILABLE"
```

## 设计取舍与扩展

- **存储**：SQLite + 进程内互斥锁，事件"顺序校验 + 插入"在单事务内完成；水平扩展或多副本时把 `app/db.py` 换成 Postgres（事件表加 `(order_id, arrived_at)` 约束即可）。
- **新增政策版本**：向 `policies` 表插入一行 JSON（时限天数、工作日/自然日、截止钟点均可配），无需发版；新增节假日在 `calendars` 表 upsert。
- **新增理赔类型/扫描事件**：在 `constants.py` 登记标签、政策 JSON 增规则、`engine.py` 增加一个 resolver，接口与代际机制无需改动。
- 截止钟点默认 23:59:59，可被政策窗口级 `cutoff` 或环境变量 `CLAIM_DEFAULT_CUTOFF` 覆盖。
- **凭证存储**：`claim_applications` 以 `(order_id, claim_type)` 唯一保证一类一证，`claim_idempotency` 以 `(order_id, idempotency_key)` 为主键记录受理/拒绝/并发落败结果——拒绝也幂等、落败键重试回放同一 409。单进程靠串行写事务裁决，多副本/多进程靠这两组唯一约束兜底，换 Postgres 时保持约束即可。
- **已归档代的受理核验**：受理路径不复用返回快照的实时判定接口，而是在当前最新代（无论是否归档）的物化事件上以 `received_at` 现算一次 `evaluate`；归档快照只作为历史凭证，不参与实时资格。
