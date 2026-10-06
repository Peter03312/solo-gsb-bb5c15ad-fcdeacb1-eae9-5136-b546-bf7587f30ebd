"""SQLite 存储层（标准库 sqlite3，无 ORM）。

单连接 + 进程内互斥锁，足以承载单容器服务；更换 Postgres 时只需重写本模块。
时间戳统一以 UTC ISO8601 字符串入库。
"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    calendar_code TEXT NOT NULL,
    date          TEXT NOT NULL,            -- YYYY-MM-DD（当地日历日期）
    kind          TEXT NOT NULL,            -- holiday | workday
    name          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (calendar_code, date)
);

CREATE TABLE IF NOT EXISTS policies (
    version     TEXT PRIMARY KEY,
    valid_from TEXT NOT NULL,               -- UTC ISO8601，含
    valid_to   TEXT,                        -- UTC ISO8601，不含；NULL 表示至今有效
    body_json  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,          -- UTC ISO8601，决定适用政策版本
    timezone        TEXT NOT NULL,
    calendar_code   TEXT NOT NULL,
    promise_by      TEXT,                   -- UTC ISO8601，可空（缺失时延迟类不可判定）
    weight_kg       REAL,
    created_policy  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    gen_no        INTEGER NOT NULL,
    opened_at     TEXT NOT NULL,            -- UTC，第一代=订单创建；后续=迟到事件到达
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   TEXT,
    supersedes_gen INTEGER,                 -- 新一代开启时，被它替代的已归档代号
    determination_json TEXT,                -- 归档时固化的判定快照（不可变）
    UNIQUE (order_id, gen_no)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    gen_id       INTEGER NOT NULL REFERENCES generations(id),
    event_type   TEXT NOT NULL,
    event_time   TEXT NOT NULL,             -- UTC ISO8601，扫描发生时间
    arrived_at   TEXT NOT NULL,             -- UTC ISO8601，服务收到时间（顺序以此为准）
    is_correction INTEGER NOT NULL DEFAULT 0,
    correction_action TEXT,                 -- amend | cancel | insert
    target_event_id TEXT,                   -- 被更正的原事件
    note         TEXT NOT NULL DEFAULT '',
    prev_arrived_at TEXT                    -- 批次内上一事件到达时间，用于报错定位
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, arrived_at);

-- 理赔申请受理凭证（一经受理不可变；同订单同理赔类型至多一张）
CREATE TABLE IF NOT EXISTS claim_applications (
    application_id      TEXT PRIMARY KEY,
    order_id            TEXT NOT NULL REFERENCES orders(order_id),
    claim_type          TEXT NOT NULL,            -- delay | damage | loss
    idempotency_key     TEXT NOT NULL,
    received_at         TEXT NOT NULL,            -- UTC ISO8601，服务实际接收时间（核验基准）
    accepted_at         TEXT NOT NULL,            -- UTC ISO8601，受理时间
    determination_code  TEXT NOT NULL,            -- 判定代号，如 DELAY_FILABLE
    generation_no       INTEGER NOT NULL,         -- 核验时订单的当前代
    policy_version      TEXT NOT NULL,            -- 下单时锁定的政策版本
    deadline_local      TEXT,                     -- 当地截止时间
    deadline_utc        TEXT,                     -- UTC 截止时间
    voucher_json        TEXT NOT NULL,            -- 不可变凭证全量（含当时判定依据）
    created_at          TEXT NOT NULL,
    UNIQUE (order_id, claim_type),                -- 同订单同类型至多一张凭证
    UNIQUE (order_id, idempotency_key)            -- 受理键在订单内唯一（与幂等表双保险）
);

-- 幂等记录：受理结果与业务拒绝结果均落表，保证同订单同键同类型重试返回原结果
CREATE TABLE IF NOT EXISTS claim_idempotency (
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    idempotency_key TEXT NOT NULL,
    claim_type      TEXT NOT NULL,                -- 首次使用该键提交的类型（换类型即冲突）
    outcome         TEXT NOT NULL,                -- accepted | rejected
    application_id  TEXT,                         -- accepted：对应凭证号
    rejection_json  TEXT,                         -- rejected：原错误体（code/message/status/details）
    received_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (order_id, idempotency_key)
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _conn = conn
    return _conn


@contextmanager
def tx():
    """串行化所有写事务，保证事件到达顺序校验与插入的原子性。"""
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    with _lock:
        return connect().execute(sql, params).fetchone()


def execute(sql: str, params=()):
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
