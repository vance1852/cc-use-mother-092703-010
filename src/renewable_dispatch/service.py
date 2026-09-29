"""联合消纳计划事务用例。

职责：

- 登记带版本的发电预测、储能可用窗口、线路能力和用户保供承诺；
- 按时间轴生成方案并冻结输入快照；
- 确认方案时锁定储能、线路与用户负荷资源；
- 预测修订时只重算未执行时段，已执行时段原样承接；
- 调度指令按方案内事件序号核对，重复/迟到回执不重复结算；
- 所有状态落 SQLite，服务重启后可恢复待执行计划。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .contracts import (
    CustomerCommitment,
    GenerationForecast,
    LineCapability,
    StorageAvailability,
    decimal_value,
    identifier,
    require_slot_boundary,
    required_text,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import (
    ZERO,
    SLOT_HOURS,
    build_schedule,
    canonical_json,
    decimal_text,
    digest,
    q,
    slot_axis,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "catalog.write",
        "forecast.write",
        "availability.write",
        "line.write",
        "commitment.write",
        "plan.generate",
        "plan.revise",
        "plan.read",
    },
    "operator": {
        "plan.confirm",
        "instruction.issue",
        "receipt.write",
        "plan.read",
    },
    "auditor": {"audit.read", "plan.read"},
}

SLOT_POWER_FIELDS = (
    "generation_kw",
    "served_kw",
    "guaranteed_kw",
    "adjustable_kw",
    "storage_charge_kw",
    "storage_discharge_kw",
    "export_kw",
    "import_kw",
    "exchange_kw",
    "renewable_curtailed_kw",
    "unmet_kw",
    "line_limit_kw",
    "line_reverse_limit_kw",
    "line_forward_margin_kw",
    "line_reverse_margin_kw",
)


class JointDispatchService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dispatch_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM dispatch_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO dispatch_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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
                    "INSERT INTO dispatch_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------ 资源台账

    def register_storage_asset(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        storage_id = identifier(raw.get("storage_id"), "storage_id")
        name = required_text(raw.get("name"), "name")
        rated_charge = decimal_value(raw.get("rated_charge_kw"), "rated_charge_kw", minimum=Decimal("0.001"))
        rated_discharge = decimal_value(raw.get("rated_discharge_kw"), "rated_discharge_kw", minimum=Decimal("0.001"))
        capacity = decimal_value(raw.get("capacity_kwh"), "capacity_kwh", minimum=Decimal("0.001"))
        efficiency = decimal_value(
            raw.get("efficiency_percent", 100), "efficiency_percent",
            minimum=Decimal("0.001"), maximum=Decimal("100"),
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO storage_assets(storage_id,name,rated_charge_kw,rated_discharge_kw,"
                    "capacity_kwh,efficiency_percent,created_at) VALUES(?,?,?,?,?,?,?)",
                    (storage_id, name, decimal_text(rated_charge), decimal_text(rated_discharge),
                     decimal_text(capacity), decimal_text(efficiency), self._now()),
                )
                self._audit("storage", storage_id, "storage.registered", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("储能编号已经存在") from exc
        return {"storage_id": storage_id, "state": "registered"}

    def register_line_asset(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        line_id = identifier(raw.get("line_id"), "line_id")
        name = required_text(raw.get("name"), "name")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO line_assets(line_id,name,created_at) VALUES(?,?,?)",
                    (line_id, name, self._now()),
                )
                self._audit("line", line_id, "line.registered", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号已经存在") from exc
        return {"line_id": line_id, "state": "registered"}

    # ------------------------------------------------------------ 版本输入

    def record_forecast(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "forecast.write")
        forecast = GenerationForecast.from_dict(raw)
        points = [{"starts_at": row["starts_at"], "power_kw": decimal_text(row["power_kw"])} for row in forecast.points]
        content = canonical_json(points)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO generation_forecasts(resource_id,kind,version,points_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (forecast.resource_id, forecast.kind, forecast.version, content, actor_id, self._now()),
                )
                forecast_id = int(cursor.lastrowid)
                self._audit("forecast", forecast.resource_id, "forecast.versioned", actor_id,
                            {"forecast_id": forecast_id, "version": forecast.version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一资源的预测版本已经存在") from exc
        return {"forecast_id": forecast_id, "resource_id": forecast.resource_id,
                "version": forecast.version, "points": len(points)}

    def record_storage_availability(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "availability.write")
        availability = StorageAvailability.from_dict(raw)
        asset = self.connection.execute(
            "SELECT capacity_kwh FROM storage_assets WHERE storage_id=?", (availability.storage_id,)
        ).fetchone()
        if asset is None:
            raise NotFound("储能资源不存在")
        if availability.initial_soc_kwh > Decimal(str(asset["capacity_kwh"])):
            raise ValidationFailed("initial_soc_kwh 不能超过储能容量")
        maintenance = [dict(item) for item in availability.maintenance]
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO storage_availabilities(storage_id,version,window_start,window_end,"
                    "initial_soc_kwh,maintenance_json,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        availability.storage_id,
                        availability.version,
                        utc_text(availability.window_start),
                        utc_text(availability.window_end),
                        decimal_text(availability.initial_soc_kwh),
                        canonical_json(maintenance),
                        actor_id,
                        self._now(),
                    ),
                )
                availability_id = int(cursor.lastrowid)
                self._audit("storage", availability.storage_id, "storage.availability_versioned", actor_id,
                            {"availability_id": availability_id, "version": availability.version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一储能的可用窗口版本已经存在") from exc
        return {"availability_id": availability_id, "storage_id": availability.storage_id,
                "version": availability.version}

    def record_line_capability(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "line.write")
        capability = LineCapability.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM line_assets WHERE line_id=?", (capability.line_id,)
        ).fetchone() is None:
            raise NotFound("线路不存在")
        points = [{"starts_at": row["starts_at"], "power_kw": decimal_text(row["power_kw"])} for row in capability.points]
        maintenance = [dict(item) for item in capability.maintenance]
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO line_capabilities(line_id,version,points_json,reverse_limit_kw,"
                    "maintenance_json,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        capability.line_id,
                        capability.version,
                        canonical_json(points),
                        decimal_text(capability.reverse_limit_kw),
                        canonical_json(maintenance),
                        actor_id,
                        self._now(),
                    ),
                )
                capability_id = int(cursor.lastrowid)
                self._audit("line", capability.line_id, "line.capability_versioned", actor_id,
                            {"capability_id": capability_id, "version": capability.version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一线路的能力版本已经存在") from exc
        return {"capability_id": capability_id, "line_id": capability.line_id,
                "version": capability.version}

    def record_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        commitment = CustomerCommitment.from_dict(raw)
        points = [
            {
                "starts_at": row["starts_at"],
                "guaranteed_kw": decimal_text(row["guaranteed_kw"]),
                "max_demand_kw": decimal_text(row["max_demand_kw"]),
                "curtailable_kw": decimal_text(row["curtailable_kw"]),
            }
            for row in commitment.points
        ]
        content = {"mode": "series", "points": points}
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO customer_commitments(customer_id,version,points_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?)",
                    (commitment.customer_id, commitment.version, canonical_json(content), actor_id, self._now()),
                )
                commitment_id = int(cursor.lastrowid)
                self._audit("commitment", commitment.customer_id, "commitment.versioned", actor_id,
                            {"commitment_id": commitment_id, "version": commitment.version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一用户的保供承诺版本已经存在") from exc
        return {"commitment_id": commitment_id, "customer_id": commitment.customer_id,
                "version": commitment.version, "points": len(points)}

    # ------------------------------------------------------------ 输入解析

    def _resolve_forecast(self, resource_id: str, version: str | None) -> dict[str, Any]:
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM generation_forecasts WHERE resource_id=? ORDER BY forecast_id DESC LIMIT 1",
                (resource_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM generation_forecasts WHERE resource_id=? AND version=?",
                (resource_id, version),
            ).fetchone()
        if row is None:
            raise NotFound(f"新能源资源 {resource_id} 的预测版本不存在")
        return dict(row)

    def _resolve_availability(self, storage_id: str, version: str | None) -> sqlite3.Row:
        sql = "SELECT * FROM storage_availabilities WHERE storage_id=?"
        params: list[Any] = [storage_id]
        if version is not None:
            sql += " AND version=?"
            params.append(version)
        else:
            sql += " ORDER BY availability_id DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"储能 {storage_id} 的可用窗口版本不存在")
        return row

    def _resolve_capability(self, line_id: str, version: str | None) -> sqlite3.Row:
        sql = "SELECT * FROM line_capabilities WHERE line_id=?"
        params: list[Any] = [line_id]
        if version is not None:
            sql += " AND version=?"
            params.append(version)
        else:
            sql += " ORDER BY capability_id DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"线路 {line_id} 的能力版本不存在")
        return row

    def _resolve_commitment(self, customer_id: str, version: str | None) -> dict[str, Any]:
        sql = "SELECT * FROM customer_commitments WHERE customer_id=?"
        params: list[Any] = [customer_id]
        if version is not None:
            sql += " AND version=?"
            params.append(version)
        else:
            sql += " ORDER BY commitment_id DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"用户 {customer_id} 的保供承诺版本不存在")
        return dict(row)

    @staticmethod
    def _refs(items: object) -> list[dict[str, str]]:
        if not isinstance(items, list):
            raise ValidationFailed("资源引用必须是数组")
        refs: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping) or not item.get("id"):
                raise ValidationFailed("资源引用必须包含 id")
            refs.append({"id": str(item["id"]), "version": item["version"] if item.get("version") else None})
        return refs

    @staticmethod
    def _require_coverage(series: Mapping[str, Decimal], slots: list[str], label: str) -> None:
        missing = [slot for slot in slots if slot not in series]
        if missing:
            raise ValidationFailed(f"{label} 未覆盖时段 {missing[0]}")

    def _build_inputs(
        self,
        horizon_start: str,
        horizon_end: str,
        resource_refs: list[dict[str, str]],
        storage_refs: list[dict[str, str]],
        line_ref: dict[str, str],
        customer_refs: list[dict[str, str]],
        opening_soc_override: Mapping[str, Decimal] | None = None,
    ) -> dict[str, Any]:
        slots = slot_axis(horizon_start, horizon_end)

        resources: list[dict[str, Any]] = []
        snapshot_resources: dict[str, Any] = {}
        for ref in resource_refs:
            row = self._resolve_forecast(ref["id"], ref["version"])
            points = json.loads(row["points_json"])
            series = {item["starts_at"]: Decimal(str(item["power_kw"])) for item in points}
            self._require_coverage(series, slots, f"资源 {ref['id']} 预测(v{row['version']})")
            resources.append({
                "resource_id": ref["id"],
                "points": [{"starts_at": slot, "power_kw": decimal_text(series[slot])} for slot in slots],
            })
            snapshot_resources[ref["id"]] = {"version": row["version"], "kind": row["kind"], "points": resources[-1]["points"]}

        customers: list[dict[str, Any]] = []
        snapshot_customers: dict[str, Any] = {}
        for ref in customer_refs:
            row = self._resolve_commitment(ref["id"], ref["version"])
            stored = json.loads(row["points_json"])
            if stored.get("mode") == "constant":
                constant = stored["constant"]
                points = [
                    {
                        "starts_at": slot,
                        "guaranteed_kw": constant["guaranteed_kw"],
                        "max_demand_kw": constant["max_demand_kw"],
                    }
                    for slot in slots
                ]
            else:
                raw_points = stored["points"]
                series = {item["starts_at"]: item for item in raw_points}
                self._require_coverage(series, slots, f"用户 {ref['id']} 保供承诺(v{row['version']})")
                points = [
                    {
                        "starts_at": slot,
                        "guaranteed_kw": series[slot]["guaranteed_kw"],
                        "max_demand_kw": series[slot]["max_demand_kw"],
                    }
                    for slot in slots
                ]
            customers.append({"customer_id": ref["id"], "points": points})
            snapshot_customers[ref["id"]] = {"version": row["version"], "points": points}

        storages: list[dict[str, Any]] = []
        snapshot_storages: dict[str, Any] = {}
        opening_soc: dict[str, Decimal] = {}
        for ref in storage_refs:
            availability = self._resolve_availability(ref["id"], ref["version"])
            asset = self.connection.execute(
                "SELECT * FROM storage_assets WHERE storage_id=?", (ref["id"],)
            ).fetchone()
            if asset is None:
                raise NotFound(f"储能 {ref['id']} 台账不存在")
            window_start = availability["window_start"]
            window_end = availability["window_end"]
            if window_start > horizon_start or window_end < horizon_end:
                raise ValidationFailed(
                    f"储能 {ref['id']} 可用窗口({window_start}~{window_end}) 未覆盖方案时间轴"
                )
            maintenance = json.loads(availability["maintenance_json"])
            initial = Decimal(str(availability["initial_soc_kwh"]))
            meta = {
                "storage_id": ref["id"],
                "rated_charge_kw": asset["rated_charge_kw"],
                "rated_discharge_kw": asset["rated_discharge_kw"],
                "capacity_kwh": asset["capacity_kwh"],
                "efficiency_percent": asset["efficiency_percent"],
                "initial_soc_kwh": decimal_text(initial),
                "window_start": window_start,
                "window_end": window_end,
                "maintenance": maintenance,
                "availability_version": availability["version"],
            }
            storages.append(meta)
            snapshot_storages[ref["id"]] = {"version": availability["version"], **meta}
            opening_soc[ref["id"]] = (
                opening_soc_override[ref["id"]] if opening_soc_override and ref["id"] in opening_soc_override else initial
            )

        capability = self._resolve_capability(line_ref["id"], line_ref["version"])
        line_points = json.loads(capability["points_json"])
        limits = {item["starts_at"]: Decimal(str(item["power_kw"])) for item in line_points}
        self._require_coverage(limits, slots, f"线路 {line_ref['id']} 能力(v{capability['version']})")
        maintenance = json.loads(capability["maintenance_json"])
        line = {
            "line_id": line_ref["id"],
            "limits": {slot: decimal_text(limits[slot]) for slot in slots},
            "reverse_limit_kw": capability["reverse_limit_kw"],
            "maintenance": maintenance,
            "version": capability["version"],
        }
        snapshot_line = {
            "line_id": line_ref["id"],
            "version": capability["version"],
            "reverse_limit_kw": capability["reverse_limit_kw"],
            "points": [{"starts_at": slot, "power_kw": line["limits"][slot]} for slot in slots],
            "maintenance": maintenance,
        }

        snapshot = {
            "horizon": [horizon_start, horizon_end],
            "resources": snapshot_resources,
            "storages": snapshot_storages,
            "line": snapshot_line,
            "customers": snapshot_customers,
        }
        return {
            "resources": resources,
            "customers": customers,
            "storages": storages,
            "line": line,
            "opening_soc": opening_soc,
            "snapshot": snapshot,
        }

    # ------------------------------------------------------------ 方案生成

    def generate_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        plan_id = identifier(raw.get("plan_id"), "plan_id")
        horizon_start_dt = require_slot_boundary(raw.get("horizon_start"), "horizon_start")
        horizon_end_dt = require_slot_boundary(raw.get("horizon_end"), "horizon_end")
        if horizon_end_dt <= horizon_start_dt:
            raise ValidationFailed("horizon_end 必须晚于 horizon_start")
        horizon_start = horizon_start_dt.isoformat().replace("+00:00", "Z")
        horizon_end = horizon_end_dt.isoformat().replace("+00:00", "Z")

        resource_refs = self._refs(raw.get("resources", []))
        if not resource_refs:
            raise ValidationFailed("方案至少要包含一个新能源资源")
        storage_refs = self._refs(raw.get("storages", []))
        customer_refs = self._refs(raw.get("customers", []))
        for label, refs in (("新能源资源", resource_refs), ("储能", storage_refs), ("用户", customer_refs)):
            ids = [ref["id"] for ref in refs]
            if len(ids) != len(set(ids)):
                raise ValidationFailed(f"{label}引用存在重复编号")
        line_raw = raw.get("line")
        if not isinstance(line_raw, Mapping) or not line_raw.get("id"):
            raise ValidationFailed("方案必须指定外送线路")
        line_ref = {"id": str(line_raw["id"]), "version": line_raw["version"] if line_raw.get("version") else None}

        if self.connection.execute("SELECT 1 FROM dispatch_plans WHERE plan_id=?", (plan_id,)).fetchone():
            raise Conflict("方案编号已经存在")

        inputs = self._build_inputs(horizon_start, horizon_end, resource_refs, storage_refs, line_ref, customer_refs)
        snapshot = inputs["snapshot"]
        input_sha256 = digest(snapshot)
        result = build_schedule(
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            resources=inputs["resources"],
            customers=inputs["customers"],
            storages=inputs["storages"],
            line=inputs["line"],
            opening_soc=inputs["opening_soc"],
        )
        with transaction(self.connection, immediate=True):
            self._persist_plan(
                plan_id=plan_id,
                horizon_start=horizon_start,
                horizon_end=horizon_end,
                snapshot=snapshot,
                input_sha256=input_sha256,
                result=result,
                slot_states={slot["starts_at"]: "pending" for slot in result["slots"]},
                actor_id=actor_id,
                revision=1,
            )
            self._audit("plan", plan_id, "plan.generated", actor_id, {"input_sha256": input_sha256})
        return {"plan_id": plan_id, "state": "draft", "revision": 1,
                "input_sha256": input_sha256, "summary": result["summary"]}

    def _persist_plan(
        self,
        *,
        plan_id: str,
        horizon_start: str,
        horizon_end: str,
        snapshot: Mapping[str, Any],
        input_sha256: str,
        result: Mapping[str, Any],
        slot_states: Mapping[str, str],
        actor_id: str,
        revision: int,
        supersedes: str | None = None,
        freeze_before: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO dispatch_plans(plan_id,horizon_start,horizon_end,state,revision,supersedes_plan_id,"
            "input_snapshot_json,input_sha256,result_json,freeze_before,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id, horizon_start, horizon_end, "draft", revision, supersedes,
                canonical_json(snapshot), input_sha256, canonical_json(result), freeze_before,
                actor_id, self._now(),
            ),
        )
        for kind, sources in (
            ("forecast", snapshot["resources"]),
            ("storage", snapshot["storages"]),
            ("line", {"__line__": snapshot["line"]}),
            ("commitment", snapshot["customers"]),
        ):
            for source_id, content in sources.items():
                real_id = snapshot["line"]["line_id"] if kind == "line" else source_id
                self.connection.execute(
                    "INSERT INTO plan_inputs(plan_id,input_kind,source_id,version,content_json,content_sha256) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, kind, real_id, str(content.get("version", "-")),
                     canonical_json(content), digest(content)),
                )
        for slot in result["slots"]:
            detail = {
                "customers": slot["customers"],
                "storages": slot["storages"],
                "reasons": slot["reasons"],
            }
            self.connection.execute(
                "INSERT INTO plan_slots(plan_id,starts_at,state," + ",".join(SLOT_POWER_FIELDS) + ",detail_json) "
                "VALUES(?,?,?," + ",".join("?" for _ in SLOT_POWER_FIELDS) + ",?)",
                (plan_id, slot["starts_at"], slot_states[slot["starts_at"]])
                + tuple(slot[field] for field in SLOT_POWER_FIELDS)
                + (canonical_json(detail),),
            )

    # ------------------------------------------------------------ 确认锁定

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan = self._plan_row(plan_id)
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前草稿版本")
        slot_rows = self.connection.execute(
            "SELECT * FROM plan_slots WHERE plan_id=? ORDER BY starts_at", (plan_id,)
        ).fetchall()
        locks: list[tuple[str, str, str, Decimal, str]] = []
        for slot in slot_rows:
            if slot["state"] == "executed":
                continue
            starts_at = slot["starts_at"]
            detail = json.loads(slot["detail_json"])
            for storage_id, storage in detail["storages"].items():
                charge = Decimal(storage["charge_kw"])
                discharge = Decimal(storage["discharge_kw"])
                if charge > ZERO:
                    locks.append(("storage", storage_id, starts_at, charge, "charge"))
                elif discharge > ZERO:
                    locks.append(("storage", storage_id, starts_at, discharge, "discharge"))
            for customer_id, customer in detail["customers"].items():
                served = Decimal(customer["served_kw"])
                if served > ZERO:
                    locks.append(("customer", customer_id, starts_at, served, "load"))
            exported = Decimal(slot["export_kw"])
            imported = Decimal(slot["import_kw"])
            if exported > ZERO:
                locks.append(("line", self._line_id(plan), starts_at, exported, "export"))
            elif imported > ZERO:
                locks.append(("line", self._line_id(plan), starts_at, imported, "import"))
        with transaction(self.connection, immediate=True):
            for resource_type, resource_id, starts_at, reserve_kw, direction in locks:
                self._ensure_lockable(resource_type, resource_id, starts_at, reserve_kw, direction, plan_id, slot_rows)
            for resource_type, resource_id, starts_at, reserve_kw, direction in locks:
                self.connection.execute(
                    "INSERT INTO resource_locks(plan_id,resource_type,resource_id,starts_at,reserve_kw,direction,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (plan_id, resource_type, resource_id, starts_at, decimal_text(reserve_kw), direction, self._now()),
                )
            cursor = self.connection.execute(
                "UPDATE dispatch_plans SET state='confirmed',confirmed_by=?,confirmed_at=? "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (actor_id, self._now(), plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案状态已变化，确认失败")
            self.connection.execute(
                "UPDATE plan_slots SET state='locked' WHERE plan_id=? AND state='pending'",
                (plan_id,),
            )
            self._audit("plan", plan_id, "plan.confirmed", actor_id,
                        {"revision": expected_revision, "locks": len(locks)})
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision, "locks": len(locks)}

    def _line_id(self, plan: sqlite3.Row) -> str:
        snapshot = json.loads(plan["input_snapshot_json"])
        return str(snapshot["line"]["line_id"])

    def _ensure_lockable(
        self,
        resource_type: str,
        resource_id: str,
        starts_at: str,
        reserve_kw: Decimal,
        direction: str,
        plan_id: str,
        slot_rows: Sequence[sqlite3.Row] | None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT reserve_kw,direction,plan_id FROM resource_locks "
            "WHERE resource_type=? AND resource_id=? AND starts_at=? AND plan_id<>?",
            (resource_type, resource_id, starts_at, plan_id),
        ).fetchall()
        if resource_type in {"storage", "customer"}:
            active = [row for row in existing if self._plan_state(row["plan_id"]) == "confirmed"]
            if active:
                raise Conflict(f"{resource_type} {resource_id} 在 {starts_at} 已被方案 {active[0]['plan_id']} 锁定")
            return
        # 线路按方向做能力累加校验（只统计仍有效的已确认方案锁）。
        plan_slot = next(row for row in slot_rows if row["starts_at"] == starts_at)
        limit = Decimal(plan_slot["line_limit_kw"] if direction == "export" else plan_slot["line_reverse_limit_kw"])
        active_rows = [row for row in existing if self._plan_state(row["plan_id"]) == "confirmed"]
        used = sum((Decimal(row["reserve_kw"]) for row in active_rows if row["direction"] == direction), ZERO)
        if used + reserve_kw > limit:
            raise Conflict(
                f"线路 {resource_id} 在 {starts_at} 的{('外送' if direction == 'export' else '受入')}能力不足："
                f"已锁定 {decimal_text(used)}，本方案需要 {decimal_text(reserve_kw)}，上限 {decimal_text(limit)}"
            )

    def _plan_state(self, plan_id: str) -> str:
        row = self.connection.execute("SELECT state FROM dispatch_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return "" if row is None else row["state"]

    # ------------------------------------------------------------ 预测修订

    def revise_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.revise")
        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("只有已确认方案可以修订重算")
        snapshot = json.loads(plan["input_snapshot_json"])
        executed_rows = self.connection.execute(
            "SELECT * FROM plan_slots WHERE plan_id=? AND state='executed' ORDER BY starts_at",
            (plan_id,),
        ).fetchall()
        pending_rows = self.connection.execute(
            "SELECT * FROM plan_slots WHERE plan_id=? AND state<>'executed' ORDER BY starts_at",
            (plan_id,),
        ).fetchall()
        if not pending_rows:
            raise InvalidState("方案全部时段已执行，无需修订")
        freeze_before = pending_rows[0]["starts_at"]

        resource_refs = [
            {"id": resource_id, "version": None} for resource_id in sorted(snapshot["resources"])
        ]
        storage_refs = [
            {"id": storage_id, "version": None} for storage_id in sorted(snapshot["storages"])
        ]
        customer_refs = [
            {"id": customer_id, "version": None} for customer_id in sorted(snapshot["customers"])
        ]
        line_ref = {"id": str(snapshot["line"]["line_id"]), "version": None}

        # 以最后一个已执行时段的结束 SOC 作为重算边界。
        opening_soc: dict[str, Decimal] = {}
        if executed_rows:
            boundary_detail = json.loads(executed_rows[-1]["detail_json"])
            opening_soc = {
                storage_id: Decimal(str(storage["soc_end_kwh"]))
                for storage_id, storage in boundary_detail["storages"].items()
            }
        inputs = self._build_inputs(
            freeze_before, plan["horizon_end"],
            resource_refs, storage_refs, line_ref, customer_refs,
            opening_soc_override=opening_soc or None,
        )
        tail = build_schedule(
            horizon_start=freeze_before,
            horizon_end=plan["horizon_end"],
            resources=inputs["resources"],
            customers=inputs["customers"],
            storages=inputs["storages"],
            line=inputs["line"],
            opening_soc=inputs["opening_soc"],
        )
        executed_slots = [self._slot_dict(row) for row in executed_rows]
        merged_slots = executed_slots + tail["slots"]
        result = {
            "horizon_start": plan["horizon_start"],
            "horizon_end": plan["horizon_end"],
            "slot_minutes": 15,
            "slots": merged_slots,
            "summary": self._summarize(merged_slots),
            "closing_soc": tail["closing_soc"],
            "revised_from": plan_id,
            "freeze_before": freeze_before,
        }
        child_snapshot = dict(inputs["snapshot"])
        child_snapshot["executed_input_sha256"] = plan["input_sha256"]
        child_snapshot["freeze_before"] = freeze_before
        child_sha256 = digest(child_snapshot)
        revision = int(plan["revision"]) + 1
        child_id = f"{plan_id}:r{revision}"
        slot_states = {
            slot["starts_at"]: ("executed" if slot["starts_at"] < freeze_before else "pending")
            for slot in merged_slots
        }
        with transaction(self.connection, immediate=True):
            if self.connection.execute("SELECT 1 FROM dispatch_plans WHERE plan_id=?", (child_id,)).fetchone():
                raise Conflict("修订方案编号冲突")
            self._persist_plan(
                plan_id=child_id,
                horizon_start=plan["horizon_start"],
                horizon_end=plan["horizon_end"],
                snapshot=child_snapshot,
                input_sha256=child_sha256,
                result=result,
                slot_states=slot_states,
                actor_id=actor_id,
                revision=revision,
                supersedes=plan_id,
                freeze_before=freeze_before,
            )
            self.connection.execute(
                "UPDATE dispatch_plans SET state='superseded',revision=? WHERE plan_id=? AND state='confirmed'",
                (revision, plan_id),
            )
            # 释放旧方案未执行时段的资源锁；已执行锁保留作为历史占用。
            self.connection.execute(
                "DELETE FROM resource_locks WHERE plan_id=? AND starts_at>=?",
                (plan_id, freeze_before),
            )
            self._audit("plan", child_id, "plan.revised", actor_id,
                        {"supersedes": plan_id, "freeze_before": freeze_before, "input_sha256": child_sha256})
        return {"plan_id": child_id, "state": "draft", "revision": revision,
                "supersedes": plan_id, "freeze_before": freeze_before,
                "input_sha256": child_sha256, "summary": result["summary"]}

    @staticmethod
    def _slot_dict(row: sqlite3.Row) -> dict[str, Any]:
        detail = json.loads(row["detail_json"])
        slot = {"state": row["state"]}
        slot.update({field: row[field] for field in SLOT_POWER_FIELDS})
        slot.update({
            "starts_at": row["starts_at"],
            "customers": detail["customers"],
            "storages": detail["storages"],
            "reasons": detail["reasons"],
        })
        return slot

    @staticmethod
    def _summarize(slots: list[dict[str, Any]]) -> dict[str, str]:
        totals = {
            "generation_kwh": ZERO,
            "served_kwh": ZERO,
            "unmet_kwh": ZERO,
            "curtailed_kwh": ZERO,
            "charge_kwh": ZERO,
            "discharge_kwh": ZERO,
            "export_kwh": ZERO,
            "import_kwh": ZERO,
        }
        firm = ZERO
        for slot in slots:
            firm += Decimal(str(slot["guaranteed_kw"]))
            totals["generation_kwh"] += Decimal(str(slot["generation_kw"]))
            totals["served_kwh"] += Decimal(str(slot["served_kw"]))
            totals["unmet_kwh"] += Decimal(str(slot["unmet_kw"]))
            totals["curtailed_kwh"] += Decimal(str(slot["renewable_curtailed_kw"]))
            totals["charge_kwh"] += Decimal(str(slot["storage_charge_kw"]))
            totals["discharge_kwh"] += Decimal(str(slot["storage_discharge_kw"]))
            totals["export_kwh"] += Decimal(str(slot["export_kw"]))
            totals["import_kwh"] += Decimal(str(slot["import_kw"]))
        firm_kwh = firm * SLOT_HOURS
        unmet_kwh = totals["unmet_kwh"] * SLOT_HOURS
        summary = {key: decimal_text(q(value * SLOT_HOURS)) for key, value in totals.items()}
        fulfillment = Decimal("100") if firm_kwh == ZERO else q((firm_kwh - unmet_kwh) / firm_kwh * 100)
        summary["guarantee_fulfillment_percent"] = format(fulfillment.quantize(Decimal("0.0001")), "f")
        return summary

    # ------------------------------------------------------------ 调度指令

    def issue_instruction(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "instruction.issue")
        plan_id = identifier(raw.get("plan_id"), "plan_id")
        slot_start_dt = require_slot_boundary(raw.get("slot_start"), "slot_start")
        slot_start = slot_start_dt.isoformat().replace("+00:00", "Z")
        resource_type = required_text(raw.get("resource_type"), "resource_type", 16)
        if resource_type not in {"storage", "line", "customer"}:
            raise ValidationFailed("resource_type 必须是 storage、line 或 customer")
        resource_id = identifier(raw.get("resource_id"), "resource_id")
        action = required_text(raw.get("action"), "action", 32)
        power_kw = decimal_value(raw.get("power_kw"), "power_kw", minimum=Decimal("0.001"))
        idem_key = identifier(raw.get("idempotency_key"), "idempotency_key")

        stored = self.connection.execute(
            "SELECT response_json FROM dispatch_idempotency WHERE scope='instruction.issue' AND idempotency_key=?",
            (idem_key,),
        ).fetchone()
        if stored is not None:
            return json.loads(stored["response_json"])

        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("只有已确认方案可以下发调度指令")
        slot = self.connection.execute(
            "SELECT * FROM plan_slots WHERE plan_id=? AND starts_at=?",
            (plan_id, slot_start),
        ).fetchone()
        if slot is None:
            raise NotFound("方案时段不存在")
        if slot["state"] != "locked":
            raise InvalidState(f"时段 {slot_start} 状态为 {slot['state']}，不能下发指令")
        detail = json.loads(slot["detail_json"])
        self._validate_action(resource_type, resource_id, action, power_kw, slot, detail)

        duplicate = self.connection.execute(
            "SELECT 1 FROM dispatch_instructions WHERE plan_id=? AND slot_start=? AND resource_type=? "
            "AND resource_id=? AND action=? AND state<>'rejected'",
            (plan_id, slot_start, resource_type, resource_id, action),
        ).fetchone()
        if duplicate is not None:
            raise Conflict("该时段的同类指令已经存在")

        response: dict[str, Any]
        with transaction(self.connection, immediate=True):
            last = self.connection.execute(
                "SELECT coalesce(max(seq_no),0) AS last_seq FROM dispatch_instructions WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            seq_no = int(last["last_seq"]) + 1
            cursor = self.connection.execute(
                "INSERT INTO dispatch_instructions(plan_id,slot_start,seq_no,resource_type,resource_id,"
                "action,power_kw,idempotency_key,issued_by,issued_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, slot_start, seq_no, resource_type, resource_id, action,
                 decimal_text(power_kw), idem_key, actor_id, self._now()),
            )
            instruction_id = int(cursor.lastrowid)
            response = {
                "instruction_id": instruction_id,
                "plan_id": plan_id,
                "seq_no": seq_no,
                "slot_start": slot_start,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "action": action,
                "power_kw": decimal_text(power_kw),
                "state": "issued",
            }
            self.connection.execute(
                "INSERT INTO dispatch_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('instruction.issue',?,?,?,?)",
                (idem_key, digest(raw), canonical_json(response), self._now()),
            )
            self._audit("instruction", str(instruction_id), "instruction.issued", actor_id,
                        {"plan_id": plan_id, "seq_no": seq_no})
        return response

    @staticmethod
    def _validate_action(
        resource_type: str,
        resource_id: str,
        action: str,
        power_kw: Decimal,
        slot: sqlite3.Row,
        detail: Mapping[str, Any],
    ) -> None:
        if resource_type == "storage":
            if action not in {"charge", "discharge"}:
                raise ValidationFailed("储能指令动作必须是 charge 或 discharge")
            storage = detail["storages"].get(resource_id)
            if storage is None:
                raise NotFound("方案中没有该储能的时段安排")
            planned = Decimal(storage["charge_kw" if action == "charge" else "discharge_kw"])
            if power_kw > planned:
                raise Conflict(f"指令功率 {decimal_text(power_kw)} 超过方案安排 {decimal_text(planned)}")
        elif resource_type == "line":
            if action not in {"export", "import"}:
                raise ValidationFailed("线路指令动作必须是 export 或 import")
            planned = Decimal(slot["export_kw" if action == "export" else "import_kw"])
            if power_kw > planned:
                raise Conflict(f"指令功率 {decimal_text(power_kw)} 超过方案安排 {decimal_text(planned)}")
        else:
            if action != "load":
                raise ValidationFailed("用户负荷指令动作必须是 load")
            customer = detail["customers"].get(resource_id)
            if customer is None:
                raise NotFound("方案中没有该用户的时段安排")
            planned = Decimal(customer["served_kw"])
            if power_kw > planned:
                raise Conflict(f"指令功率 {decimal_text(power_kw)} 超过方案安排 {decimal_text(planned)}")

    def _load_instruction(self, instruction_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dispatch_instructions WHERE instruction_id=?", (instruction_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调度指令不存在")
        return row

    def acknowledge_instruction(
        self, actor_id: str, instruction_id: int, receipt_seq_no: int, idempotency_key: str
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        return self._register_receipt(
            actor_id, instruction_id, receipt_seq_no, idempotency_key, "ack", measured_kwh=None
        )

    def settle_instruction(
        self,
        actor_id: str,
        instruction_id: int,
        receipt_seq_no: int,
        idempotency_key: str,
        measured_kwh: object = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        energy = None
        if measured_kwh is not None:
            energy = decimal_value(measured_kwh, "measured_kwh", minimum=Decimal("0"))
        return self._register_receipt(
            actor_id, instruction_id, receipt_seq_no, idempotency_key, "settlement", measured_kwh=energy
        )

    def _register_receipt(
        self,
        actor_id: str,
        instruction_id: int,
        receipt_seq_no: int,
        idempotency_key: str,
        kind: str,
        *,
        measured_kwh: Decimal | None,
    ) -> dict[str, Any]:
        identifier(idempotency_key, "idempotency_key")
        if isinstance(receipt_seq_no, bool) or not isinstance(receipt_seq_no, int) or receipt_seq_no <= 0:
            raise ValidationFailed("receipt_seq_no 必须是正整数")
        stored = self.connection.execute(
            "SELECT response_json FROM dispatch_idempotency WHERE scope=:scope AND idempotency_key=:key",
            {"scope": f"receipt.{kind}", "key": idempotency_key},
        ).fetchone()
        if stored is not None:
            # 幂等重放：不产生任何状态变化，不重复结算；被拒绝的回执重放同样的错误。
            replay = json.loads(stored["response_json"])
            if replay.get("note") == "事件序号不匹配":
                raise Conflict(
                    f"回执事件序号 {replay['receipt_seq_no']} 与指令序号 {replay['seq_no']} 不一致"
                )
            if replay.get("note") == "指令尚未确认回执，不能结算":
                raise InvalidState(replay["note"])
            return replay

        instruction = self._load_instruction(instruction_id)
        current_state = instruction["state"]
        accepted = False
        note = ""
        settle_kwh: Decimal | None = None
        event_type = "receipt.duplicate"

        if receipt_seq_no != int(instruction["seq_no"]):
            note = "事件序号不匹配"
            event_type = "receipt.sequence_mismatch"
        elif kind == "ack":
            if current_state == "issued":
                accepted, note, event_type = True, "回执确认", "receipt.acknowledged"
            elif current_state == "acknowledged":
                note = "重复回执"
            elif current_state == "settled":
                note = "迟到回执"
            else:
                note = f"指令状态为 {current_state}"
        else:
            if current_state == "settled":
                note = "重复结算"
            elif current_state == "rejected":
                note = "指令已作废"
            elif current_state == "issued":
                note = "指令尚未确认回执，不能结算"
                event_type = "receipt.settlement_rejected"
            else:  # acknowledged
                settle_kwh = (
                    Decimal(instruction["power_kw"]) * SLOT_HOURS
                    if measured_kwh is None
                    else measured_kwh
                )
                accepted, note, event_type = True, "结算完成", "receipt.settled"

        # 同序号同类回执已登记（即便使用了不同幂等键）：按重复处理，不再结算。
        duplicate_receipt = self.connection.execute(
            "SELECT 1 FROM instruction_receipts WHERE instruction_id=? AND receipt_seq_no=? AND kind=?",
            (instruction_id, receipt_seq_no, kind),
        ).fetchone()

        response = {
            "instruction_id": instruction_id,
            "seq_no": int(instruction["seq_no"]),
            "receipt_seq_no": receipt_seq_no,
            "kind": kind,
            "accepted": accepted,
            "note": note,
            "state": current_state,
        }
        if settle_kwh is not None:
            response["settled_kwh"] = decimal_text(settle_kwh)

        with transaction(self.connection, immediate=True):
            if duplicate_receipt is None:
                self.connection.execute(
                    "INSERT INTO instruction_receipts(instruction_id,receipt_seq_no,kind,accepted,note,"
                    "idempotency_key,received_by,received_at) VALUES(?,?,?,?,?,?,?,?)",
                    (instruction_id, receipt_seq_no, kind, 1 if accepted else 0, note,
                     idempotency_key, actor_id, self._now()),
                )
            self._store_receipt_response(kind, idempotency_key, response, instruction_id, receipt_seq_no)
            self._audit("instruction", str(instruction_id), event_type, actor_id,
                        {"accepted": accepted, "note": note,
                         "receipt_seq_no": receipt_seq_no, "expected": instruction["seq_no"]}
                        if event_type == "receipt.sequence_mismatch"
                        else {"accepted": accepted, "note": note})

        if note == "事件序号不匹配":
            raise Conflict(f"回执事件序号 {receipt_seq_no} 与指令序号 {instruction['seq_no']} 不一致")
        if note == "指令尚未确认回执，不能结算":
            raise InvalidState(note)

        if accepted:
            with transaction(self.connection, immediate=True):
                if kind == "ack":
                    self.connection.execute(
                        "UPDATE dispatch_instructions SET state='acknowledged',ack_at=? WHERE instruction_id=?",
                        (self._now(), instruction_id),
                    )
                    response["state"] = "acknowledged"
                else:
                    self.connection.execute(
                        "UPDATE dispatch_instructions SET state='settled',settled_kwh=?,settled_at=? "
                        "WHERE instruction_id=?",
                        (decimal_text(settle_kwh), self._now(), instruction_id),
                    )
                    response["state"] = "settled"
                    self._mark_slot_executed_if_done(instruction["plan_id"], instruction["slot_start"])
        return response

    def _store_receipt_response(
        self, kind: str, idempotency_key: str, response: Mapping[str, Any],
        instruction_id: int, receipt_seq_no: int,
    ) -> None:
        self.connection.execute(
            "INSERT INTO dispatch_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (f"receipt.{kind}", idempotency_key,
             digest({"instruction_id": instruction_id, "receipt_seq_no": receipt_seq_no}),
             canonical_json(response), self._now()),
        )

    def complete_slot(self, actor_id: str, plan_id: str, slot_start: str) -> dict[str, Any]:
        """确认无调度动作的空闲时段已经过去，可作为修订边界。"""
        self._require(actor_id, "instruction.issue")
        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("只有已确认方案可以核销时段")
        slot_start = require_slot_boundary(slot_start, "slot_start").isoformat().replace("+00:00", "Z")
        slot = self.connection.execute(
            "SELECT state FROM plan_slots WHERE plan_id=? AND starts_at=?",
            (plan_id, slot_start),
        ).fetchone()
        if slot is None:
            raise NotFound("方案时段不存在")
        if slot["state"] == "executed":
            return {"plan_id": plan_id, "starts_at": slot_start, "state": "executed"}
        if slot["state"] != "locked":
            raise InvalidState(f"时段状态为 {slot['state']}，不能核销")
        open_count = self.connection.execute(
            "SELECT count(*) FROM dispatch_instructions WHERE plan_id=? AND slot_start=? "
            "AND state IN ('issued','acknowledged')",
            (plan_id, slot_start),
        ).fetchone()[0]
        if open_count:
            raise InvalidState("该时段仍有未闭环调度指令")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE plan_slots SET state='executed' WHERE plan_id=? AND starts_at=? AND state='locked'",
                (plan_id, slot_start),
            )
            self._audit("plan", plan_id, "slot.completed", actor_id, {"starts_at": slot_start})
        return {"plan_id": plan_id, "starts_at": slot_start, "state": "executed"}

    def _mark_slot_executed_if_done(self, plan_id: str, slot_start: str) -> None:
        slot = self.connection.execute(
            "SELECT detail_json,export_kw,import_kw FROM plan_slots WHERE plan_id=? AND starts_at=?",
            (plan_id, slot_start),
        ).fetchone()
        if slot is None:
            return
        detail = json.loads(slot["detail_json"])
        expected = 0
        for storage in detail["storages"].values():
            if Decimal(str(storage["charge_kw"])) > ZERO or Decimal(str(storage["discharge_kw"])) > ZERO:
                expected += 1
        for customer in detail["customers"].values():
            if Decimal(str(customer["served_kw"])) > ZERO:
                expected += 1
        if Decimal(slot["export_kw"]) > ZERO or Decimal(slot["import_kw"]) > ZERO:
            expected += 1
        settled = self.connection.execute(
            "SELECT count(*) FROM dispatch_instructions WHERE plan_id=? AND slot_start=? AND state='settled'",
            (plan_id, slot_start),
        ).fetchone()[0]
        open_count = self.connection.execute(
            "SELECT count(*) FROM dispatch_instructions WHERE plan_id=? AND slot_start=? "
            "AND state IN ('issued','acknowledged')",
            (plan_id, slot_start),
        ).fetchone()[0]
        if expected > 0 and open_count == 0 and settled >= expected:
            self.connection.execute(
                "UPDATE plan_slots SET state='executed' WHERE plan_id=? AND starts_at=? AND state='locked'",
                (plan_id, slot_start),
            )

    def instruction(self, actor_id: str, instruction_id: int) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self._load_instruction(instruction_id)
        result = dict(row)
        result["receipts"] = [
            dict(item) for item in self.connection.execute(
                "SELECT receipt_seq_no,kind,accepted,note,received_at FROM instruction_receipts "
                "WHERE instruction_id=? ORDER BY receipt_id",
                (instruction_id,),
            ).fetchall()
        ]
        return result

    # ------------------------------------------------------------ 运营视图

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM dispatch_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def plan_detail(self, actor_id: str | None, plan_id: str) -> dict[str, Any]:
        if actor_id is not None:
            self._require(actor_id, "plan.read")
        plan = self._plan_row(plan_id)
        slots = [
            self._slot_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM plan_slots WHERE plan_id=? ORDER BY starts_at", (plan_id,)
            ).fetchall()
        ]
        result = json.loads(plan["result_json"])
        inputs = [
            dict(row) for row in self.connection.execute(
                "SELECT input_kind,source_id,version,content_sha256 FROM plan_inputs "
                "WHERE plan_id=? ORDER BY input_kind,source_id",
                (plan_id,),
            ).fetchall()
        ]
        return {
            "plan_id": plan_id,
            "state": plan["state"],
            "revision": plan["revision"],
            "horizon_start": plan["horizon_start"],
            "horizon_end": plan["horizon_end"],
            "supersedes_plan_id": plan["supersedes_plan_id"],
            "freeze_before": plan["freeze_before"],
            "input_sha256": plan["input_sha256"],
            "confirmed_at": plan["confirmed_at"],
            "inputs": inputs,
            "summary": result.get("summary", self._summarize(slots)),
            "slots": slots,
        }

    def list_plans(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        rows = self.connection.execute(
            "SELECT plan_id,state,revision,horizon_start,horizon_end,supersedes_plan_id,freeze_before,"
            "input_sha256,created_at FROM dispatch_plans ORDER BY created_at,plan_id"
        ).fetchall()
        return {"plans": [dict(row) for row in rows]}

    def pending_work(self, actor_id: str) -> dict[str, Any]:
        """返回重启后仍待执行的计划时段与未闭环指令。"""
        self._require(actor_id, "plan.read")
        plan_rows = self.connection.execute(
            "SELECT plan_id,horizon_start,horizon_end FROM dispatch_plans WHERE state='confirmed' ORDER BY plan_id"
        ).fetchall()
        plans: list[dict[str, Any]] = []
        for plan in plan_rows:
            slot_rows = self.connection.execute(
                "SELECT * FROM plan_slots WHERE plan_id=? AND state='locked' ORDER BY starts_at",
                (plan["plan_id"],),
            ).fetchall()
            open_instructions = self.connection.execute(
                "SELECT instruction_id,slot_start,seq_no,resource_type,resource_id,action,power_kw,state "
                "FROM dispatch_instructions WHERE plan_id=? AND state IN ('issued','acknowledged') "
                "ORDER BY seq_no",
                (plan["plan_id"],),
            ).fetchall()
            plans.append({
                "plan_id": plan["plan_id"],
                "horizon_start": plan["horizon_start"],
                "horizon_end": plan["horizon_end"],
                "pending_slots": [
                    {
                        "starts_at": row["starts_at"],
                        "line_forward_margin_kw": row["line_forward_margin_kw"],
                        "line_reverse_margin_kw": row["line_reverse_margin_kw"],
                        "unmet_kw": row["unmet_kw"],
                        "renewable_curtailed_kw": row["renewable_curtailed_kw"],
                        "reasons": json.loads(row["detail_json"])["reasons"],
                    }
                    for row in slot_rows
                ],
                "open_instructions": [dict(row) for row in open_instructions],
            })
        return {"confirmed_plans": plans}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM dispatch_audit_events ORDER BY event_id").fetchall()
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
