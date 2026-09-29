"""联合消纳计划事务用例：版本化输入、分时方案、资源锁定与指令结算。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .contracts import GenerationForecast, LineCapability, LoadCommitment, StorageWindow
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import (
    LineSpec,
    LoadSpec,
    PlanInput,
    StorageSpec,
    build_plan,
    canonical_json,
    decimal_text,
    digest,
    replay_snapshot,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "forecast.write", "storage.write", "line.write", "load.write", "horizon.write", "plan.generate",
    },
    "dispatcher": {"plan.confirm", "report.read"},
    "operator": {"receipt.write"},
    "auditor": {"report.read", "audit.read"},
}

STORAGE_ORDER = ("storage", "line", "load")


class DispatchService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rd_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM rd_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO rd_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rd_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_horizon(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "horizon.write")
        horizon_id = str(raw.get("horizon_id", "")).strip()
        if not horizon_id:
            raise ValidationFailed("horizon_id 不能为空")
        starts_at = str(raw.get("starts_at", "")).strip()
        try:
            parse_utc(starts_at, "starts_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        interval = raw.get("interval_minutes", 60)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0 or 1440 % interval != 0:
            raise ValidationFailed("interval_minutes 必须是整除 1440 的正整数")
        slot_count = raw.get("slot_count")
        if isinstance(slot_count, bool) or not isinstance(slot_count, int) or slot_count <= 0:
            raise ValidationFailed("slot_count 必须是正整数")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO horizons(horizon_id,starts_at,interval_minutes,slot_count,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (horizon_id, starts_at, interval, slot_count, actor_id, self._now()),
                )
                self._audit("horizon", horizon_id, "horizon.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("调度时段已经存在") from exc
        return {"horizon_id": horizon_id, "slot_count": slot_count, "interval_minutes": interval}

    # ----------------------------------------------------------- 版本化输入

    def record_forecast(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "forecast.write")
        forecast = GenerationForecast.from_dict(raw)
        previous = self.connection.execute(
            "SELECT forecast_id FROM generation_forecasts WHERE resource_id=? AND state='active' "
            "ORDER BY version DESC LIMIT 1",
            (forecast.resource_id,),
        ).fetchone()
        if previous is not None and previous["forecast_id"] == forecast.forecast_id:
            raise Conflict("同一预测编号不能重复登记")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE generation_forecasts SET state='retired' WHERE resource_id=? AND state='active'",
                    (forecast.resource_id,),
                )
                self.connection.execute(
                    "INSERT INTO generation_forecasts(forecast_id,resource_id,resource_kind,version,"
                    "interval_minutes,power_json,observed_at,supersedes_forecast_id,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        forecast.forecast_id,
                        forecast.resource_id,
                        forecast.resource_kind,
                        forecast.version,
                        forecast.interval_minutes,
                        canonical_json([decimal_text(point) for point in forecast.power_mw]),
                        forecast.observed_at,
                        None if previous is None else previous["forecast_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("forecast", forecast.forecast_id, "forecast.recorded", actor_id,
                            {"resource_id": forecast.resource_id, "version": forecast.version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("预测编号或资源版本冲突") from exc
        return {
            "forecast_id": forecast.forecast_id,
            "resource_id": forecast.resource_id,
            "version": forecast.version,
            "state": "active",
        }

    def register_storage(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "storage.write")
        storage_id = str(raw.get("storage_id", "")).strip()
        if not storage_id:
            raise ValidationFailed("storage_id 不能为空")
        kind = str(raw.get("storage_kind", "battery")).strip().lower()
        interval = raw.get("interval_minutes", 60)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValidationFailed("interval_minutes 必须是正整数")
        fields = {}
        for name in ("capacity_mwh", "max_charge_mw", "max_discharge_mw"):
            fields[name] = decimal_text(
                _decimal(raw.get(name), name, minimum=Decimal("0"))
            )
        for name in ("charge_efficiency", "discharge_efficiency"):
            fields[name] = decimal_text(
                _decimal(raw.get(name, 1), name, minimum=Decimal("0.01"), maximum=Decimal("1"))
            )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO storage_units(storage_id,storage_kind,interval_minutes,capacity_mwh,"
                    "max_charge_mw,max_discharge_mw,charge_efficiency,discharge_efficiency,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        storage_id, kind, interval, fields["capacity_mwh"], fields["max_charge_mw"],
                        fields["max_discharge_mw"], fields["charge_efficiency"], fields["discharge_efficiency"],
                        actor_id, self._now(),
                    ),
                )
                self._audit("storage", storage_id, "storage.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("储能编号已经存在") from exc
        return {"storage_id": storage_id, "state": "registered"}

    def record_storage_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "storage.write")
        window = StorageWindow.from_dict(raw)
        unit = self._storage_unit(window.storage_id)
        if window.initial_soc_mwh > Decimal(unit["capacity_mwh"]):
            raise ValidationFailed("initial_soc_mwh 不能超过储能额定容量")
        version = _version(raw)
        previous = self._latest_version_id("storage_windows", "storage_id", window.storage_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE storage_windows SET state='retired' WHERE storage_id=? AND state='active'",
                    (window.storage_id,),
                )
                self.connection.execute(
                    "INSERT INTO storage_windows(window_id,storage_id,version,initial_soc_mwh,available_json,"
                    "reason,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"{window.storage_id}:v{version}", window.storage_id, version,
                        decimal_text(window.initial_soc_mwh),
                        canonical_json([bool(flag) for flag in window.available]),
                        window.reason, actor_id, self._now(),
                    ),
                )
                self._audit("storage_window", f"{window.storage_id}:v{version}",
                            "storage_window.recorded", actor_id,
                            {"storage_id": window.storage_id, "version": version, "reason": window.reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("储能可用窗口版本冲突") from exc
        return {"window_id": f"{window.storage_id}:v{version}", "version": version, "state": "active"}

    def register_line(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "line.write")
        line_id = str(raw.get("line_id", "")).strip()
        if not line_id:
            raise ValidationFailed("line_id 不能为空")
        interval = raw.get("interval_minutes", 60)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValidationFailed("interval_minutes 必须是正整数")
        rated = decimal_text(_decimal(raw.get("rated_mw"), "rated_mw", minimum=Decimal("0")))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO transmission_lines(line_id,interval_minutes,rated_mw,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (line_id, interval, rated, actor_id, self._now()),
                )
                self._audit("line", line_id, "line.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号已经存在") from exc
        return {"line_id": line_id, "state": "registered"}

    def record_line_capability(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "line.write")
        capability = LineCapability.from_dict(raw)
        self._line(capability.line_id)
        version = _version(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE line_capabilities SET state='retired' WHERE line_id=? AND state='active'",
                    (capability.line_id,),
                )
                self.connection.execute(
                    "INSERT INTO line_capabilities(capability_id,line_id,version,limits_json,reason,"
                    "recorded_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        f"{capability.line_id}:v{version}", capability.line_id, version,
                        canonical_json([None if value is None else decimal_text(value)
                                        for value in capability.limits_mw]),
                        capability.reason, actor_id, self._now(),
                    ),
                )
                self._audit("line_capability", f"{capability.line_id}:v{version}",
                            "line_capability.recorded", actor_id,
                            {"line_id": capability.line_id, "version": version, "reason": capability.reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路能力版本冲突") from exc
        return {"capability_id": f"{capability.line_id}:v{version}", "version": version, "state": "active"}

    def register_load(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "load.write")
        load_id = str(raw.get("load_id", "")).strip()
        if not load_id:
            raise ValidationFailed("load_id 不能为空")
        kind = str(raw.get("load_kind", "industrial")).strip().lower()
        if kind not in {"industrial", "critical"}:
            raise ValidationFailed("load_kind 必须是 industrial 或 critical")
        interval = raw.get("interval_minutes", 60)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValidationFailed("interval_minutes 必须是正整数")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO industrial_loads(load_id,load_kind,interval_minutes,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (load_id, kind, interval, actor_id, self._now()),
                )
                self._audit("load", load_id, "load.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户编号已经存在") from exc
        return {"load_id": load_id, "state": "registered"}

    def record_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "load.write")
        commitment = LoadCommitment.from_dict(raw)
        self._load(commitment.load_id)
        version = _version(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE load_commitments SET state='retired' WHERE load_id=? AND state='active'",
                    (commitment.load_id,),
                )
                self.connection.execute(
                    "INSERT INTO load_commitments(commitment_id,load_id,version,firm_json,curtailable_json,"
                    "reductions_json,reason,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        f"{commitment.load_id}:v{version}", commitment.load_id, version,
                        canonical_json([decimal_text(point) for point in commitment.firm_mw]),
                        canonical_json([decimal_text(point) for point in commitment.curtailable_mw]),
                        canonical_json([None if value is None else decimal_text(value)
                                        for value in commitment.reductions]),
                        commitment.reason, actor_id, self._now(),
                    ),
                )
                self._audit("load_commitment", f"{commitment.load_id}:v{version}",
                            "commitment.recorded", actor_id,
                            {"load_id": commitment.load_id, "version": version, "reason": commitment.reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("保供约束版本冲突") from exc
        return {"commitment_id": f"{commitment.load_id}:v{version}", "version": version, "state": "active"}

    def _storage_unit(self, storage_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM storage_units WHERE storage_id=?", (storage_id,)
        ).fetchone()
        if row is None:
            raise NotFound("储能不存在")
        return row

    def _line(self, line_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM transmission_lines WHERE line_id=?", (line_id,)
        ).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return row

    def _load(self, load_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM industrial_loads WHERE load_id=?", (load_id,)
        ).fetchone()
        if row is None:
            raise NotFound("工业用户不存在")
        return row

    def _latest_version_id(self, table: str, column: str, value: str) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT * FROM {table} WHERE {column}=? AND state='active' ORDER BY version DESC LIMIT 1",
            (value,),
        ).fetchone()

    # --------------------------------------------------------------- 方案生成

    def _horizon(self, horizon_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM horizons WHERE horizon_id=?", (horizon_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调度时段不存在")
        return row

    def _gather_inputs(
        self, horizon: sqlite3.Row, start_slot: int, slot_count: int, starts_at: str,
        storage_initial_soc: Mapping[str, Decimal] | None = None,
    ) -> PlanInput:
        n = slot_count
        interval = horizon["interval_minutes"]
        end_slot = start_slot + n

        generation: dict[str, list[Decimal]] = {}
        basis_forecasts: dict[str, dict[str, object]] = {}
        forecast_rows = self.connection.execute(
            "SELECT * FROM generation_forecasts WHERE state='active' ORDER BY resource_id"
        ).fetchall()
        if not forecast_rows:
            raise InvalidState("没有已登记的有效发电预测")
        for row in forecast_rows:
            if row["interval_minutes"] != interval:
                raise InvalidState(f"预测 {row['forecast_id']} 时间粒度与调度时段不一致")
            series = [Decimal(str(point)) for point in json.loads(row["power_json"])]
            if len(series) < end_slot:
                raise InvalidState(f"预测 {row['forecast_id']} 未覆盖整个调度时段")
            key = f"{row['resource_kind']}:{row['resource_id']}"
            generation[key] = series[start_slot:end_slot]
            basis_forecasts[key] = {"forecast_id": row["forecast_id"], "version": row["version"]}

        storage_specs: list[StorageSpec] = []
        basis_storage: dict[str, object] = {}
        for unit in self.connection.execute(
            "SELECT * FROM storage_units WHERE active=1 ORDER BY storage_id"
        ).fetchall():
            if unit["interval_minutes"] != interval:
                raise InvalidState(f"储能 {unit['storage_id']} 时间粒度与调度时段不一致")
            window = self.connection.execute(
                "SELECT * FROM storage_windows WHERE storage_id=? AND state='active' ORDER BY version DESC LIMIT 1",
                (unit["storage_id"],),
            ).fetchone()
            if window is None:
                raise InvalidState(f"储能 {unit['storage_id']} 缺少可用窗口")
            flags = [bool(flag) for flag in json.loads(window["available_json"])]
            if len(flags) < end_slot:
                raise InvalidState(f"储能窗口 {window['window_id']} 未覆盖整个调度时段")
            initial = (
                storage_initial_soc[unit["storage_id"]]
                if storage_initial_soc is not None and unit["storage_id"] in storage_initial_soc
                else Decimal(window["initial_soc_mwh"])
            )
            storage_specs.append(
                StorageSpec(
                    storage_id=unit["storage_id"],
                    capacity_mwh=Decimal(unit["capacity_mwh"]),
                    max_charge_mw=Decimal(unit["max_charge_mw"]),
                    max_discharge_mw=Decimal(unit["max_discharge_mw"]),
                    charge_efficiency=Decimal(unit["charge_efficiency"]),
                    discharge_efficiency=Decimal(unit["discharge_efficiency"]),
                    initial_soc_mwh=initial,
                    available=tuple(flags[start_slot:end_slot]),
                    reason=window["reason"],
                )
            )
            basis_storage[unit["storage_id"]] = {"window_id": window["window_id"], "version": window["version"]}

        line_specs: list[LineSpec] = []
        basis_lines: dict[str, object] = {}
        for line in self.connection.execute(
            "SELECT * FROM transmission_lines WHERE active=1 ORDER BY line_id"
        ).fetchall():
            if line["interval_minutes"] != interval:
                raise InvalidState(f"线路 {line['line_id']} 时间粒度与调度时段不一致")
            cap = self.connection.execute(
                "SELECT * FROM line_capabilities WHERE line_id=? AND state='active' ORDER BY version DESC LIMIT 1",
                (line["line_id"],),
            ).fetchone()
            if cap is None:
                limits: tuple[Decimal | None, ...] = (None,) * n
                basis_lines[line["line_id"]] = {"capability_id": None, "version": None}
            else:
                raw_limits = [None if value is None else Decimal(str(value))
                              for value in json.loads(cap["limits_json"])]
                if len(raw_limits) < end_slot:
                    raise InvalidState(f"线路能力 {cap['capability_id']} 未覆盖整个调度时段")
                limits = tuple(raw_limits[start_slot:end_slot])
                basis_lines[line["line_id"]] = {"capability_id": cap["capability_id"], "version": cap["version"]}
            line_specs.append(
                LineSpec(line_id=line["line_id"], rated_mw=Decimal(line["rated_mw"]),
                         limits_mw=limits, reason="" if cap is None else cap["reason"])
            )

        load_specs: list[LoadSpec] = []
        basis_loads: dict[str, object] = {}
        for load in self.connection.execute(
            "SELECT * FROM industrial_loads WHERE active=1 ORDER BY load_id"
        ).fetchall():
            if load["interval_minutes"] != interval:
                raise InvalidState(f"用户 {load['load_id']} 时间粒度与调度时段不一致")
            commitment = self.connection.execute(
                "SELECT * FROM load_commitments WHERE load_id=? AND state='active' ORDER BY version DESC LIMIT 1",
                (load["load_id"],),
            ).fetchone()
            if commitment is None:
                raise InvalidState(f"用户 {load['load_id']} 缺少保供约束")
            firm = [Decimal(str(point)) for point in json.loads(commitment["firm_json"])]
            curtailable = [Decimal(str(point)) for point in json.loads(commitment["curtailable_json"])]
            reductions = [None if value is None else Decimal(str(value))
                          for value in json.loads(commitment["reductions_json"])]
            if len(firm) < end_slot:
                raise InvalidState(f"保供约束 {commitment['commitment_id']} 未覆盖整个调度时段")
            load_specs.append(
                LoadSpec(
                    load_id=load["load_id"],
                    firm_mw=tuple(firm[start_slot:end_slot]),
                    curtailable_mw=tuple(curtailable[start_slot:end_slot]),
                    reductions=tuple(reductions[start_slot:end_slot]),
                    reason=commitment["reason"],
                )
            )
            basis_loads[load["load_id"]] = {
                "commitment_id": commitment["commitment_id"], "version": commitment["version"],
            }

        plan_input = PlanInput(
            horizon_id=horizon["horizon_id"],
            interval_minutes=interval,
            slot_count=n,
            starts_at=starts_at,
            generation=generation,
            storage=tuple(storage_specs),
            lines=tuple(line_specs),
            loads=tuple(load_specs),
        )
        plan_input.basis = {  # type: ignore[attr-defined]
            "forecasts": basis_forecasts,
            "storage_windows": basis_storage,
            "line_capabilities": basis_lines,
            "load_commitments": basis_loads,
        }
        return plan_input

    def generate_plan(
        self, actor_id: str, plan_id: str, horizon_id: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        horizon = self._horizon(horizon_id)
        head = self.connection.execute(
            "SELECT * FROM dispatch_plans WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
            (plan_id,),
        ).fetchone()

        with transaction(self.connection, immediate=True):
            if head is None:
                revision = 1
                plan_input = self._gather_inputs(horizon, 0, horizon["slot_count"], horizon["starts_at"])
                snapshot = plan_input.as_snapshot()
                snapshot["basis"] = plan_input.basis  # type: ignore[attr-defined]
                result = build_plan(plan_input)
                frozen_slots = 0
                frozen_result: list[dict[str, object]] = []
            else:
                if head["state"] not in {"confirmed", "closed"}:
                    raise InvalidState("只有已确认（或已关闭）的方案才能基于修订输入重算")
                revision = head["revision"] + 1
                frozen_slots = self._executed_slot_count(horizon)
                if frozen_slots >= horizon["slot_count"]:
                    raise InvalidState("调度时段已全部执行，不能再修订方案")
                if frozen_slots == 0:
                    # 尚无时段执行，按当前输入整体重算。
                    plan_input = self._gather_inputs(horizon, 0, horizon["slot_count"], horizon["starts_at"])
                    snapshot = plan_input.as_snapshot()
                    snapshot["basis"] = plan_input.basis
                    result = build_plan(plan_input)
                    frozen_result = []
                else:
                    frozen_head = json.loads(head["result_json"])
                    frozen_result = list(frozen_head["periods"][:frozen_slots])
                    # 重算起点的储能 SOC 取自已锁定方案最后一个冻结时段，保证时序连续。
                    tail = frozen_result[-1]
                    initial_soc = {
                        item["storage_id"]: Decimal(str(item["soc_mwh"]))
                        for item in tail.get("storage", [])
                    }
                    future_starts = utc_text(
                        parse_utc(horizon["starts_at"], "starts_at")
                        + timedelta(minutes=horizon["interval_minutes"] * frozen_slots)
                    )
                    plan_input = self._gather_inputs(
                        horizon, frozen_slots, horizon["slot_count"] - frozen_slots,
                        future_starts, storage_initial_soc=initial_soc,
                    )
                    future_snapshot = plan_input.as_snapshot()
                    future_snapshot["basis"] = plan_input.basis
                    future_result = build_plan(plan_input)
                    # 未来槽位编号平移到全局网格。
                    for period in future_result["periods"]:
                        period["slot"] = period["slot"] + frozen_slots
                    snapshot = {
                        "frozen_slots": frozen_slots,
                        "freeze_starts_at": future_starts,
                        "frozen_plan_revision": head["revision"],
                        "future_snapshot": future_snapshot,
                    }
                    result = self._merge_results(
                        horizon, frozen_result, future_result, revision_note=True
                    )

            snapshot_sha = digest(snapshot)
            result_sha = digest(result)
            # 按输入采用的版本号（预测/窗口/能力/保供）判断是否真的发生修订。
            input_basis_sha = digest(
                snapshot.get("basis") or snapshot.get("future_snapshot", {}).get("basis") or {}
            )
            if head is not None and digest(self._snapshot_basis(
                json.loads(head["snapshot_json"])
            )) == input_basis_sha:
                raise Conflict("当前输入与已确认方案采用的版本一致，无需重算")
            self.connection.execute(
                "INSERT INTO dispatch_plans(plan_id,revision,horizon_id,state,frozen_before,basis_json,"
                "snapshot_json,snapshot_sha256,result_json,result_sha256,revision_note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, revision, horizon_id, "draft",
                    None if frozen_slots == 0 else snapshot.get("freeze_starts_at", horizon["starts_at"]),
                    canonical_json(
                        snapshot.get("basis")
                        or snapshot.get("future_snapshot", {}).get("basis")
                        or snapshot
                    ),
                    canonical_json(snapshot), snapshot_sha,
                    canonical_json(result), result_sha, note, actor_id, self._now(),
                ),
            )
            self._audit("plan", plan_id, "plan.generated", actor_id,
                        {"revision": revision, "snapshot_sha256": snapshot_sha,
                         "result_sha256": result_sha, "frozen_slots": frozen_slots})
        return {
            "plan_id": plan_id, "revision": revision, "state": "draft",
            "snapshot_sha256": snapshot_sha, "result_sha256": result_sha,
            "frozen_slots": frozen_slots,
            "feasible": result["feasible"],
            "total_unmet_demand_mw": result["total_unmet_demand_mw"],
            "periods": result["periods"],
        }

    def _executed_slot_count(self, horizon: sqlite3.Row) -> int:
        now = self.clock.now()
        start = parse_utc(horizon["starts_at"], "starts_at")
        elapsed_minutes = (now - start).total_seconds() / 60
        slots = int(elapsed_minutes // horizon["interval_minutes"])
        return max(0, min(horizon["slot_count"], slots))

    @staticmethod
    def _merge_results(
        horizon: sqlite3.Row,
        frozen_result: list[dict[str, object]],
        future_result: dict[str, object],
        *,
        revision_note: bool,
    ) -> dict[str, object]:
        for period in frozen_result:
            period["frozen"] = True
            if revision_note and "frozen_from_revision" not in period:
                period["adjustment_reasons"] = list(period.get("adjustment_reasons", [])) + ["slot_frozen"]
        for period in future_result["periods"]:
            period["frozen"] = False
        periods = frozen_result + list(future_result["periods"])
        total_unmet = sum((Decimal(str(period["unmet_demand_mw"])) for period in periods), Decimal("0"))
        total_curtailed = sum((Decimal(str(period.get("curtailed_mw", "0"))) for period in periods), Decimal("0"))
        return {
            "horizon_id": horizon["horizon_id"],
            "interval_minutes": horizon["interval_minutes"],
            "slot_count": horizon["slot_count"],
            "feasible": total_unmet == Decimal("0"),
            "total_unmet_demand_mw": decimal_text(total_unmet.quantize(Decimal("0.001"))),
            "total_curtailed_mw": decimal_text(total_curtailed.quantize(Decimal("0.001"))),
            "periods": periods,
        }

    # --------------------------------------------------------------- 确认锁定

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        draft = self.connection.execute(
            "SELECT * FROM dispatch_plans WHERE plan_id=? AND revision=?",
            (plan_id, expected_revision),
        ).fetchone()
        if draft is None:
            raise NotFound("方案版本不存在")
        if draft["state"] != "draft":
            raise InvalidState("只有草稿方案可以确认")
        horizon = self._horizon(draft["horizon_id"])
        result = json.loads(draft["result_json"])
        resources = self._plan_resources(draft["snapshot_json"])
        # 资源若已被其他确认方案锁定，则本方案不能确认。
        holders = []
        for rtype, rid in resources:
            row = self.connection.execute(
                "SELECT plan_id,revision FROM resource_locks "
                "WHERE resource_type=? AND resource_id=? AND plan_id<>?",
                (rtype, rid, plan_id),
            ).fetchone()
            if row is not None:
                holders.append((rtype, rid, row["plan_id"], row["revision"]))
        if holders:
            raise Conflict(
                "资源已被其他确认方案锁定："
                + ",".join(f"{rtype}:{rid}->{pid}v{rev}" for rtype, rid, pid, rev in holders)
            )
        try:
            with transaction(self.connection, immediate=True):
                confirmed_at = self._now()
                updated = self.connection.execute(
                    "UPDATE dispatch_plans SET state='confirmed',confirmed_at=? "
                    "WHERE plan_id=? AND revision=? AND state='draft'",
                    (confirmed_at, plan_id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise InvalidState("方案状态已变化，确认失败")
                # 同一计划的旧确认版本让位并释放其锁定、取消其未执行指令。
                old = self.connection.execute(
                    "SELECT revision FROM dispatch_plans WHERE plan_id=? AND state='confirmed' AND revision<>?",
                    (plan_id, expected_revision),
                ).fetchall()
                for row in old:
                    self.connection.execute(
                        "UPDATE dispatch_plans SET state='superseded' WHERE plan_id=? AND revision=?",
                        (plan_id, row["revision"]),
                    )
                    self.connection.execute(
                        "UPDATE dispatch_instructions SET state='cancelled' WHERE plan_id=? AND revision=? "
                        "AND state IN ('issued','acknowledged')",
                        (plan_id, row["revision"]),
                    )
                self.connection.execute(
                    "DELETE FROM resource_locks WHERE plan_id=? AND revision<>?",
                    (plan_id, expected_revision),
                )
                # 锁定方案涉及的储能、线路与用户资源。
                for resource_type, resource_id in resources:
                    self.connection.execute(
                        "INSERT INTO resource_locks(plan_id,revision,resource_type,resource_id,locked_by,locked_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (plan_id, expected_revision, resource_type, resource_id, actor_id, confirmed_at),
                    )
                # 仅为未冻结槽位生成调度指令，事件序号在计划内全局单调延续。
                next_seq_row = self.connection.execute(
                    "SELECT coalesce(max(event_seq),0) AS max_seq FROM dispatch_instructions WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()
                next_seq = int(next_seq_row["max_seq"]) + 1
                instructions: list[dict[str, Any]] = []
                hours = Decimal(horizon["interval_minutes"]) / Decimal("60")
                for period in result["periods"]:
                    if period.get("frozen"):
                        continue
                    for item in sorted(period.get("storage", []), key=lambda s: s["storage_id"]):
                        charge = Decimal(str(item["charge_mw"]))
                        discharge = Decimal(str(item["discharge_mw"]))
                        if charge > 0:
                            action, power = "charge", charge
                        elif discharge > 0:
                            action, power = "discharge", discharge
                        else:
                            continue
                        cursor = self.connection.execute(
                            "INSERT INTO dispatch_instructions(plan_id,revision,slot,starts_at,resource_type,"
                            "resource_id,action,power_mw,event_seq,issued_by,issued_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                plan_id, expected_revision, period["slot"], period["starts_at"],
                                "storage", item["storage_id"], action, decimal_text(power),
                                next_seq, actor_id, confirmed_at,
                            ),
                        )
                        instructions.append({
                            "instruction_id": int(cursor.lastrowid),
                            "event_seq": next_seq,
                            "slot": period["slot"],
                            "starts_at": period["starts_at"],
                            "resource_id": item["storage_id"],
                            "action": action,
                            "power_mw": decimal_text(power),
                            "energy_mwh": decimal_text((power * hours).quantize(Decimal("0.001"))),
                        })
                        next_seq += 1
                self._audit("plan", plan_id, "plan.confirmed", actor_id,
                            {"revision": expected_revision, "locks": len(resources),
                             "instructions": len(instructions)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源锁定冲突，可能已有其他方案确认") from exc
        return {
            "plan_id": plan_id, "revision": expected_revision, "state": "confirmed",
            "locked_resources": [
                {"resource_type": rtype, "resource_id": rid} for rtype, rid in resources
            ],
            "instructions": instructions,
        }

    @staticmethod
    def _plan_resources(snapshot_json: str) -> list[tuple[str, str]]:
        snapshot = json.loads(snapshot_json)
        body = snapshot.get("future_snapshot", snapshot)
        resources: list[tuple[str, str]] = []
        for item in body.get("storage", []):
            resources.append(("storage", item["storage_id"]))
        for item in body.get("lines", []):
            resources.append(("line", item["line_id"]))
        for item in body.get("loads", []):
            resources.append(("load", item["load_id"]))
        return sorted(set(resources), key=lambda pair: (STORAGE_ORDER.index(pair[0]), pair[1]))

    # ----------------------------------------------------------- 回执与结算

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        plan_id = str(raw.get("plan_id", "")).strip()
        if not plan_id:
            raise ValidationFailed("plan_id 不能为空")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValidationFailed("revision 必须是整数")
        event_seq = raw.get("event_seq")
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq <= 0:
            raise ValidationFailed("event_seq 必须是正整数")
        idempotency_key = str(raw.get("idempotency_key", "")).strip()
        if not idempotency_key:
            raise ValidationFailed("idempotency_key 不能为空")
        phase = str(raw.get("phase", "settled")).strip().lower()
        if phase not in {"acknowledged", "settled"}:
            raise ValidationFailed("phase 必须是 acknowledged 或 settled")
        energy = _decimal(raw.get("settled_energy_mwh", 0), "settled_energy_mwh", minimum=Decimal("0"))
        request_digest = digest({
            "plan_id": plan_id, "revision": revision, "event_seq": event_seq,
            "phase": phase, "settled_energy_mwh": decimal_text(energy),
        })

        # 幂等键：同一键重复提交直接回放首次结果，绝不重复结算；同键不同内容则冲突。
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM rd_idempotency WHERE scope='receipt' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同回执内容")
            return json.loads(stored["response_json"])

        instruction = self.connection.execute(
            "SELECT * FROM dispatch_instructions WHERE plan_id=? AND revision=? AND event_seq=?",
            (plan_id, revision, event_seq),
        ).fetchone()
        if instruction is None:
            raise NotFound("调度指令不存在")
        plan = self.connection.execute(
            "SELECT * FROM dispatch_plans WHERE plan_id=? AND revision=?",
            (plan_id, revision),
        ).fetchone()

        outcome = "accepted"
        reason = ""
        horizon = self._horizon(plan["horizon_id"])
        slot_end = parse_utc(instruction["starts_at"], "starts_at") + timedelta(
            minutes=horizon["interval_minutes"]
        )
        now = self.clock.now()
        if plan["state"] == "superseded" or instruction["state"] == "cancelled":
            outcome, reason = "late", "方案已被新版本取代或指令已取消，迟到回执不予结算"
        elif instruction["state"] == "settled":
            outcome, reason = "late", "指令已经结算，重复回执不再结算"
        elif phase == "acknowledged":
            # 调度确认必须在时段结束前到达，否则视为迟到。
            if instruction["state"] != "issued" or now > slot_end:
                outcome, reason = "late", "确认回执超过调度时段或指令状态已变化"
        else:  # phase == "settled"：计量结算只能在该时段结束之后到达。
            if instruction["state"] not in {"issued", "acknowledged"}:
                outcome, reason = "late", "指令状态不允许结算"
            elif now < slot_end:
                outcome, reason = "late", "时段尚未结束，过早的结算回执不予受理"

        response = {
            "plan_id": plan_id, "revision": revision, "event_seq": event_seq,
            "instruction_id": instruction["instruction_id"], "outcome": outcome,
            "settled": False, "reason": reason,
        }
        with transaction(self.connection, immediate=True):
            if outcome == "accepted":
                if phase == "settled":
                    new_state = "settled"
                    response["settled"] = True
                    response["settled_energy_mwh"] = decimal_text(energy)
                else:
                    new_state = "acknowledged"
                self.connection.execute(
                    "UPDATE dispatch_instructions SET state=?,"
                    "acknowledged_at=COALESCE(acknowledged_at,?),"
                    "settled_at=CASE WHEN ?='settled' THEN ? ELSE settled_at END,"
                    "settled_energy_mwh=CASE WHEN ?='settled' THEN ? ELSE settled_energy_mwh END "
                    "WHERE instruction_id=?",
                    (
                        new_state, self._now(),
                        new_state, self._now(),
                        new_state, decimal_text(energy),
                        instruction["instruction_id"],
                    ),
                )
            self.connection.execute(
                "INSERT INTO instruction_receipts(plan_id,revision,instruction_id,event_seq,"
                "idempotency_key,request_sha256,outcome,received_by,received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, revision, instruction["instruction_id"], event_seq,
                    idempotency_key, request_digest, outcome, actor_id, self._now(),
                ),
            )
            self.connection.execute(
                "INSERT INTO rd_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('receipt',?,?,?,?)",
                (idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit("instruction", str(instruction["instruction_id"]),
                        "receipt.recorded", actor_id,
                        {"event_seq": event_seq, "outcome": outcome, "phase": phase})
        return response

    # ------------------------------------------------------------- 运营接口

    def plan_view(self, actor_id: str, plan_id: str, revision: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self._plan_row(plan_id, revision)
        result = json.loads(row["result_json"])
        snapshot = json.loads(row["snapshot_json"])
        return {
            "plan_id": plan_id,
            "revision": row["revision"],
            "state": row["state"],
            "horizon_id": row["horizon_id"],
            "frozen_before": row["frozen_before"],
            "snapshot_sha256": row["snapshot_sha256"],
            "result_sha256": row["result_sha256"],
            "basis": self._snapshot_basis(snapshot),
            "feasible": result["feasible"],
            "total_unmet_demand_mw": result["total_unmet_demand_mw"],
            "total_curtailed_mw": result.get("total_curtailed_mw", "0"),
            "periods": result["periods"],
        }

    def operations_view(self, actor_id: str, horizon_id: str) -> dict[str, Any]:
        """面向运营人员：当前方案各时段约束余量、未满足需求与调整原因。"""
        self._require(actor_id, "report.read")
        horizon = self._horizon(horizon_id)
        row = self.connection.execute(
            "SELECT * FROM dispatch_plans WHERE horizon_id=? ORDER BY "
            "CASE state WHEN 'confirmed' THEN 0 WHEN 'draft' THEN 1 WHEN 'superseded' THEN 2 ELSE 3 END,"
            "revision DESC LIMIT 1",
            (horizon_id,),
        ).fetchone()
        if row is None:
            raise NotFound("该调度时段尚无方案")
        view = self.plan_view(actor_id, row["plan_id"], row["revision"])
        executed = self._executed_slot_count(horizon)
        periods = []
        for period in view["periods"]:
            periods.append({
                "slot": period["slot"],
                "starts_at": period["starts_at"],
                "status": "executed" if period["slot"] < executed else "pending",
                "generation_mw": period["generation_mw"],
                "target_load_mw": period["target_load_mw"],
                "storage_charge_mw": period["storage_charge_mw"],
                "storage_discharge_mw": period["storage_discharge_mw"],
                "net_balance_mw": period["net_balance_mw"],
                "unmet_demand_mw": period["unmet_demand_mw"],
                "curtailed_mw": period.get("curtailed_mw", "0"),
                "line_margins": period["line_margins"],
                "adjustment_reasons": period["adjustment_reasons"],
                "frozen": bool(period.get("frozen", period["slot"] < executed)),
            })
        pending = self.connection.execute(
            "SELECT event_seq,slot,starts_at,resource_id,action,power_mw,state "
            "FROM dispatch_instructions WHERE plan_id=? AND revision=? AND state IN ('issued','acknowledged') "
            "ORDER BY event_seq",
            (row["plan_id"], row["revision"]),
        ).fetchall()
        return {
            "horizon_id": horizon_id,
            "plan_id": row["plan_id"],
            "revision": row["revision"],
            "state": row["state"],
            "now": self._now(),
            "executed_slots": executed,
            "periods": periods,
            "pending_instructions": [dict(item) for item in pending],
        }

    def _plan_row(self, plan_id: str, revision: int | None) -> sqlite3.Row:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM dispatch_plans WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM dispatch_plans WHERE plan_id=? AND revision=?",
                (plan_id, revision),
            ).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    @staticmethod
    def _snapshot_basis(snapshot: Mapping[str, Any]) -> dict[str, object]:
        if "basis" in snapshot:
            return snapshot["basis"]  # type: ignore[return-value]
        future = snapshot.get("future_snapshot")
        if isinstance(future, Mapping) and "basis" in future:
            return future["basis"]  # type: ignore[return-value]
        return {}

    def verify_snapshot(self, actor_id: str, plan_id: str, revision: int) -> dict[str, Any]:
        """用保存的输入快照重算并比对结果摘要，证明快照可复现方案。"""
        self._require(actor_id, "audit.read")
        row = self._plan_row(plan_id, revision)
        snapshot = json.loads(row["snapshot_json"])
        stored_result = json.loads(row["result_json"])
        if "future_snapshot" in snapshot:
            future = replay_snapshot(snapshot["future_snapshot"])
            frozen_slots = int(snapshot["frozen_slots"])
            for period in future["periods"]:
                period["slot"] = period["slot"] + frozen_slots
            replayed = {
                "periods": stored_result["periods"][:frozen_slots] + list(future["periods"]),
            }
            replayed_matches = all(
                replayed["periods"][i]["unmet_demand_mw"] == stored_result["periods"][i]["unmet_demand_mw"]
                and replayed["periods"][i]["net_balance_mw"] == stored_result["periods"][i]["net_balance_mw"]
                for i in range(len(stored_result["periods"]))
            )
        else:
            replayed = replay_snapshot(snapshot)
            replayed_matches = digest(replayed) == row["result_sha256"]
        return {
            "plan_id": plan_id, "revision": revision,
            "snapshot_sha256": row["snapshot_sha256"],
            "result_sha256": row["result_sha256"],
            "replay_matches": bool(replayed_matches),
        }

    def recover_pending(self, actor_id: str | None = None) -> dict[str, Any]:
        """服务重启后从 SQLite 恢复所有待执行的确认方案与指令。"""
        if actor_id is not None:
            self._require(actor_id, "report.read")
        plans = self.connection.execute(
            "SELECT plan_id,revision,horizon_id,confirmed_at FROM dispatch_plans "
            "WHERE state='confirmed' ORDER BY plan_id"
        ).fetchall()
        recovered = []
        for plan in plans:
            instructions = self.connection.execute(
                "SELECT event_seq,slot,starts_at,resource_type,resource_id,action,power_mw,state "
                "FROM dispatch_instructions WHERE plan_id=? AND revision=? "
                "AND state IN ('issued','acknowledged') ORDER BY event_seq",
                (plan["plan_id"], plan["revision"]),
            ).fetchall()
            recovered.append({
                "plan_id": plan["plan_id"],
                "revision": plan["revision"],
                "horizon_id": plan["horizon_id"],
                "confirmed_at": plan["confirmed_at"],
                "pending_instructions": [dict(item) for item in instructions],
            })
        return {"recovered_at": self._now(), "plans": recovered}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM rd_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}


def _decimal(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def _version(raw: Mapping[str, Any]) -> int:
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValidationFailed("version 必须是正整数")
    return version
