"""联合消纳服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dispatch_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','operator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 带版本的新能源发电预测（版本为不可变快照，修订即新版本）。
CREATE TABLE IF NOT EXISTS generation_forecasts (
    forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('solar','wind')),
    version TEXT NOT NULL,
    points_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(resource_id, version)
);

CREATE INDEX IF NOT EXISTS idx_forecast_resource
ON generation_forecasts(resource_id, forecast_id);

-- 储能资源台账（容量与额定参数），可用性随方案快照冻结。
CREATE TABLE IF NOT EXISTS storage_assets (
    storage_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rated_charge_kw TEXT NOT NULL,
    rated_discharge_kw TEXT NOT NULL,
    capacity_kwh TEXT NOT NULL,
    efficiency_percent TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 输变电线路台账。
CREATE TABLE IF NOT EXISTS line_assets (
    line_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 带版本的储能可用窗口（检修窗口随版本冻结）。
CREATE TABLE IF NOT EXISTS storage_availabilities (
    availability_id INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_id TEXT NOT NULL REFERENCES storage_assets(storage_id),
    version TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    initial_soc_kwh TEXT NOT NULL,
    maintenance_json TEXT NOT NULL DEFAULT '[]',
    recorded_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(storage_id, version)
);

-- 带版本的线路分时输送能力。
CREATE TABLE IF NOT EXISTS line_capabilities (
    capability_id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id TEXT NOT NULL REFERENCES line_assets(line_id),
    version TEXT NOT NULL,
    points_json TEXT NOT NULL,
    reverse_limit_kw TEXT NOT NULL,
    maintenance_json TEXT NOT NULL DEFAULT '[]',
    recorded_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(line_id, version)
);

-- 工业用户保供承诺（带版本）。
CREATE TABLE IF NOT EXISTS customer_commitments (
    commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    version TEXT NOT NULL,
    points_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(customer_id, version)
);

-- 联合消纳方案主表。
CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id TEXT PRIMARY KEY,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','superseded','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    supersedes_plan_id TEXT REFERENCES dispatch_plans(plan_id),
    input_snapshot_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    freeze_before TEXT,
    recalculated_at TEXT,
    created_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES dispatch_users(user_id),
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_state_time
ON dispatch_plans(state, horizon_start);

-- 方案采用的逐版本输入快照（独立成行，便于审计与重放）。
CREATE TABLE IF NOT EXISTS plan_inputs (
    plan_id TEXT NOT NULL REFERENCES dispatch_plans(plan_id) DEFERRABLE INITIALLY DEFERRED,
    input_kind TEXT NOT NULL CHECK(input_kind IN ('forecast','storage','line','commitment')),
    source_id TEXT NOT NULL,
    version TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY(plan_id, input_kind, source_id)
);

-- 方案时段行：约束余量、未满足需求与调整原因。
CREATE TABLE IF NOT EXISTS plan_slots (
    plan_id TEXT NOT NULL REFERENCES dispatch_plans(plan_id) DEFERRABLE INITIALLY DEFERRED,
    starts_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','executed','locked')),
    generation_kw TEXT NOT NULL,
    guaranteed_kw TEXT NOT NULL,
    served_kw TEXT NOT NULL,
    adjustable_kw TEXT NOT NULL,
    storage_charge_kw TEXT NOT NULL,
    storage_discharge_kw TEXT NOT NULL,
    export_kw TEXT NOT NULL,
    import_kw TEXT NOT NULL,
    exchange_kw TEXT NOT NULL,
    renewable_curtailed_kw TEXT NOT NULL,
    unmet_kw TEXT NOT NULL,
    line_limit_kw TEXT NOT NULL,
    line_reverse_limit_kw TEXT NOT NULL,
    line_forward_margin_kw TEXT NOT NULL,
    line_reverse_margin_kw TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, starts_at)
);

CREATE INDEX IF NOT EXISTS idx_plan_slots_state
ON plan_slots(plan_id, state, starts_at);

-- 已确认方案锁定的资源时段（储能充放、线路通道、用户负荷）。
CREATE TABLE IF NOT EXISTS resource_locks (
    lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES dispatch_plans(plan_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('storage','line','customer')),
    resource_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    reserve_kw TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('charge','discharge','export','import','load')),
    created_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, starts_at, plan_id)
);

CREATE INDEX IF NOT EXISTS idx_locks_resource_time
ON resource_locks(resource_type, resource_id, starts_at);

-- 调度指令：按方案内单调事件序号核对。
CREATE TABLE IF NOT EXISTS dispatch_instructions (
    instruction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES dispatch_plans(plan_id),
    slot_start TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('storage','line','customer')),
    resource_id TEXT NOT NULL,
    action TEXT NOT NULL,
    power_kw TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','acknowledged','settled','rejected')),
    idempotency_key TEXT NOT NULL UNIQUE,
    issued_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    issued_at TEXT NOT NULL,
    ack_at TEXT,
    settled_kwh TEXT,
    settled_at TEXT,
    UNIQUE(plan_id, seq_no)
);

CREATE INDEX IF NOT EXISTS idx_instructions_plan_seq
ON dispatch_instructions(plan_id, seq_no);

-- 回执登记：重复或迟到回执被识别且不重复结算。
CREATE TABLE IF NOT EXISTS instruction_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id INTEGER NOT NULL REFERENCES dispatch_instructions(instruction_id),
    receipt_seq_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('ack','settlement')),
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    received_by TEXT NOT NULL REFERENCES dispatch_users(user_id),
    received_at TEXT NOT NULL,
    UNIQUE(instruction_id, receipt_seq_no, kind)
);

-- 幂等结果表。
CREATE TABLE IF NOT EXISTS dispatch_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

-- 哈希链审计事件。
CREATE TABLE IF NOT EXISTS dispatch_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_audit_entity
ON dispatch_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
