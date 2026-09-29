"""联合消纳计划服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rd_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rd_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','operator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horizons (
    horizon_id TEXT PRIMARY KEY,
    starts_at TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0 AND 1440 % interval_minutes = 0),
    slot_count INTEGER NOT NULL CHECK(slot_count > 0),
    created_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL
);

-- 带版本的新能源发电预测
CREATE TABLE IF NOT EXISTS generation_forecasts (
    forecast_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    resource_kind TEXT NOT NULL CHECK(resource_kind IN ('wind','solar')),
    version INTEGER NOT NULL CHECK(version > 0),
    interval_minutes INTEGER NOT NULL,
    power_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_forecast_id TEXT REFERENCES generation_forecasts(forecast_id),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    recorded_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(resource_id, version)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_resource
ON generation_forecasts(resource_id, state, version);

CREATE TABLE IF NOT EXISTS storage_units (
    storage_id TEXT PRIMARY KEY,
    storage_kind TEXT NOT NULL CHECK(storage_kind IN ('battery','thermal')),
    interval_minutes INTEGER NOT NULL,
    capacity_mwh TEXT NOT NULL,
    max_charge_mw TEXT NOT NULL,
    max_discharge_mw TEXT NOT NULL,
    charge_efficiency TEXT NOT NULL,
    discharge_efficiency TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL
);

-- 储能可用窗口（检修/可用性）按版本登记
CREATE TABLE IF NOT EXISTS storage_windows (
    window_id TEXT PRIMARY KEY,
    storage_id TEXT NOT NULL REFERENCES storage_units(storage_id),
    version INTEGER NOT NULL CHECK(version > 0),
    initial_soc_mwh TEXT NOT NULL,
    available_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    recorded_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(storage_id, version)
);

CREATE TABLE IF NOT EXISTS transmission_lines (
    line_id TEXT PRIMARY KEY,
    interval_minutes INTEGER NOT NULL,
    rated_mw TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL
);

-- 输变电线路分时能力（检修降额）按版本登记
CREATE TABLE IF NOT EXISTS line_capabilities (
    capability_id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES transmission_lines(line_id),
    version INTEGER NOT NULL CHECK(version > 0),
    limits_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    recorded_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(line_id, version)
);

CREATE TABLE IF NOT EXISTS industrial_loads (
    load_id TEXT PRIMARY KEY,
    load_kind TEXT NOT NULL CHECK(load_kind IN ('industrial','critical')),
    interval_minutes INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL
);

-- 用户保供约束按版本登记（firm/curtailable/承诺变化）
CREATE TABLE IF NOT EXISTS load_commitments (
    commitment_id TEXT PRIMARY KEY,
    load_id TEXT NOT NULL REFERENCES industrial_loads(load_id),
    version INTEGER NOT NULL CHECK(version > 0),
    firm_json TEXT NOT NULL,
    curtailable_json TEXT NOT NULL,
    reductions_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    recorded_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(load_id, version)
);

-- 分时方案：同一 horizon 下按 revision 演进，确认后锁定资源
CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    horizon_id TEXT NOT NULL REFERENCES horizons(horizon_id),
    state TEXT NOT NULL CHECK(state IN ('draft','confirmed','superseded','closed')),
    frozen_before TEXT,
    basis_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    revision_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES rd_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    PRIMARY KEY(plan_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_plans_state
ON dispatch_plans(state, horizon_id);

-- 确认时锁定的资源
CREATE TABLE IF NOT EXISTS resource_locks (
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('storage','line','load')),
    resource_id TEXT NOT NULL,
    locked_by TEXT NOT NULL REFERENCES rd_users(user_id),
    locked_at TEXT NOT NULL,
    PRIMARY KEY(resource_type, resource_id),
    FOREIGN KEY(plan_id, revision) REFERENCES dispatch_plans(plan_id, revision)
);

-- 调度指令：每个方案槽位/资源一条，按事件序号核对
CREATE TABLE IF NOT EXISTS dispatch_instructions (
    instruction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    slot INTEGER NOT NULL CHECK(slot >= 0),
    starts_at TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('charge','discharge','hold','curtail','import')),
    power_mw TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','acknowledged','settled','cancelled')),
    issued_by TEXT NOT NULL REFERENCES rd_users(user_id),
    issued_at TEXT NOT NULL,
    acknowledged_at TEXT,
    settled_at TEXT,
    settled_energy_mwh TEXT,
    UNIQUE(plan_id, revision, event_seq),
    FOREIGN KEY(plan_id, revision) REFERENCES dispatch_plans(plan_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_instructions_slot
ON dispatch_instructions(plan_id, revision, slot);

-- 设备/用户回执：事件序号唯一，重复或迟到回执不重复结算
CREATE TABLE IF NOT EXISTS instruction_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    instruction_id INTEGER NOT NULL REFERENCES dispatch_instructions(instruction_id),
    event_seq INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('accepted','duplicate','late','mismatch')),
    received_by TEXT NOT NULL REFERENCES rd_users(user_id),
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rd_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS rd_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_rd_audit_entity
ON rd_audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "rd_schema_meta", "rd_users", "horizons", "generation_forecasts",
    "storage_units", "storage_windows", "transmission_lines", "line_capabilities",
    "industrial_loads", "load_commitments", "dispatch_plans", "resource_locks",
    "dispatch_instructions", "instruction_receipts", "rd_idempotency", "rd_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务为每个请求使用线程，连接在 WAL + BEGIN IMMEDIATE 下可跨线程共享。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO rd_schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM rd_schema_meta WHERE key='schema_version'"
    ).fetchone()
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    }
