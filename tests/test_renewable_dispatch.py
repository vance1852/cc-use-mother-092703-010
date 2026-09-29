from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from renewable_dispatch.api import JsonApplication
from renewable_dispatch.clock import FrozenClock
from renewable_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from renewable_dispatch.planning import build_schedule, slot_axis
from renewable_dispatch.service import JointDispatchService
from renewable_dispatch.storage import connect


START = "2026-09-29T00:00:00Z"


def points(values, *, prefix="power_kw"):
    rows = []
    for index, value in enumerate(values):
        rows.append({"starts_at": f"2026-09-29T00:{15 * index:02d}:00Z", prefix: str(value)})
    return rows


def commitment_rows(guaranteed, max_demand):
    return [
        {"starts_at": f"2026-09-29T00:{15 * index:02d}:00Z",
         "guaranteed_kw": str(g), "max_demand_kw": str(m), "curtailable_kw": "0"}
        for index, (g, m) in enumerate(zip(guaranteed, max_demand))
    ]


class PlanningEngineTests(unittest.TestCase):
    def _schedule(self, *, gen, guaranteed=(), max_demand=(), forward=300, reverse=100,
                  soc=100, storage_maintenance=(), line_maintenance=()):
        slots = slot_axis(START, "2026-09-29T01:00:00Z")
        resources = [{"resource_id": "pv-1", "points": [
            {"starts_at": s, "power_kw": str(v)} for s, v in zip(slots, gen)
        ]}]
        customers = [{"customer_id": "ind-1", "points": [
            {"starts_at": s, "guaranteed_kw": str(g), "max_demand_kw": str(m)}
            for s, g, m in zip(slots, guaranteed or [0] * len(slots), max_demand or [0] * len(slots))
        ]}]
        storages = [{
            "storage_id": "ess-1",
            "rated_charge_kw": "200", "rated_discharge_kw": "200",
            "capacity_kwh": "400", "efficiency_percent": "100",
            "initial_soc_kwh": str(soc),
            "window_start": START, "window_end": "2026-09-29T01:00:00Z",
            "maintenance": storage_maintenance,
        }]
        line = {
            "line_id": "line-1",
            "limits": {s: str(forward) for s in slots},
            "reverse_limit_kw": str(reverse),
            "maintenance": line_maintenance,
        }
        return build_schedule(
            horizon_start=START, horizon_end="2026-09-29T01:00:00Z",
            resources=resources, customers=customers, storages=storages, line=line,
            opening_soc={"ess-1": Decimal(str(soc))},
        )

    def test_deficit_uses_storage_then_import_then_unmet(self) -> None:
        result = self._schedule(gen=(0, 0, 0, 0), guaranteed=(300, 300, 300, 300), max_demand=(300,) * 4)
        first, second = result["slots"][0], result["slots"][1]
        self.assertEqual(first["storage_discharge_kw"], "200.000")
        self.assertEqual(first["import_kw"], "100.000")
        self.assertEqual(first["unmet_kw"], "0.000")
        # 第一时段放空 50kWh 后，第二时段储能只能再放 50kWh(200kW)。
        self.assertEqual(second["storage_discharge_kw"], "200.000")
        self.assertEqual(second["import_kw"], "100.000")
        self.assertEqual(second["unmet_kw"], "0.000")
        # 第三时段储能已空、受入打满：出现未满足需求。
        self.assertEqual(result["slots"][2]["unmet_kw"], "200.000")
        self.assertIn("unmet_demand", result["slots"][2]["reasons"])

    def test_surplus_charges_then_raises_load_then_exports_then_curtails(self) -> None:
        result = self._schedule(gen=(800, 800, 0, 0), guaranteed=(300, 300, 0, 0), max_demand=(500, 500, 0, 0))
        first = result["slots"][0]
        self.assertEqual(first["storage_charge_kw"], "200.000")
        self.assertEqual(first["adjustable_kw"], "200.000")
        self.assertEqual(first["export_kw"], "100.000")
        self.assertEqual(first["renewable_curtailed_kw"], "0.000")
        self.assertEqual(first["line_forward_margin_kw"], "200.000")

    def test_line_maintenance_forces_curtailment_and_unmet(self) -> None:
        window = [{"starts_at": START, "ends_at": "2026-09-29T00:15:00Z", "reason": "线路检修"}]
        result = self._schedule(
            gen=(0, 0, 0, 0), guaranteed=(300, 0, 0, 0), max_demand=(300, 0, 0, 0),
            line_maintenance=window,
        )
        first = result["slots"][0]
        self.assertEqual(first["line_forward_margin_kw"], "0.000")
        self.assertEqual(first["import_kw"], "0.000")
        self.assertEqual(first["unmet_kw"], "100.000")
        self.assertIn("line_maintenance", first["reasons"])

    def test_storage_maintenance_skips_resource(self) -> None:
        window = [{"starts_at": START, "ends_at": "2026-09-29T01:00:00Z", "reason": "储能检修"}]
        result = self._schedule(
            gen=(0, 0, 0, 0), guaranteed=(100, 0, 0, 0), max_demand=(100, 0, 0, 0),
            storage_maintenance=window,
        )
        storage = result["slots"][0]["storages"]["ess-1"]
        self.assertFalse(storage["available"])
        self.assertEqual(storage["state"], "idle")
        self.assertEqual(storage["reason"], "storage_maintenance")


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc))
        self.service = JointDispatchService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("ops", "operator"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.register_storage_asset("plan", {
            "storage_id": "ess-1", "name": "储能一号",
            "rated_charge_kw": "200", "rated_discharge_kw": "200",
            "capacity_kwh": "400", "efficiency_percent": "100",
        })
        self.service.register_line_asset("plan", {"line_id": "line-1", "name": "衡阳北通道"})

    def tearDown(self) -> None:
        self.connection.close()

    def seed_world(self, forecast_values=(0, 50, 200, 800), forecast_version="v1",
                   guaranteed=(300, 300, 300, 300), max_demand=(500, 500, 500, 500)) -> None:
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": forecast_version,
            "points": points(forecast_values),
        })
        self.service.record_storage_availability("plan", {
            "storage_id": "ess-1", "version": "w1",
            "window_start": START, "window_end": "2026-09-29T01:00:00Z",
            "initial_soc_kwh": "100", "maintenance": [],
        })
        self.service.record_line_capability("plan", {
            "line_id": "line-1", "version": "c1",
            "points": points((300, 300, 300, 300)),
            "reverse_limit_kw": "100", "maintenance": [],
        })
        self.service.record_commitment("plan", {
            "customer_id": "ind-1", "version": "g1",
            "points": commitment_rows(guaranteed, max_demand),
        })

    def make_plan(self, plan_id="plan-1"):
        return self.service.generate_plan("plan", {
            "plan_id": plan_id,
            "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
            "resources": [{"id": "pv-1"}],
            "storages": [{"id": "ess-1"}],
            "line": {"id": "line-1"},
            "customers": [{"id": "ind-1"}],
        })


class PlanLifecycleTests(ServiceTestBase):
    def test_plan_freezes_versioned_input_snapshot(self) -> None:
        self.seed_world()
        plan = self.make_plan()
        self.assertEqual(len(plan["input_sha256"]), 64)
        detail = self.service.plan_detail("audit", "plan-1")
        versions = {(row["input_kind"], row["source_id"]): row["version"] for row in detail["inputs"]}
        self.assertEqual(versions[("forecast", "pv-1")], "v1")
        self.assertEqual(versions[("storage", "ess-1")], "w1")
        self.assertEqual(versions[("line", "line-1")], "c1")
        self.assertEqual(versions[("commitment", "ind-1")], "g1")
        # 登记新版本预测后，已生成方案的快照仍然是 v1。
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": "v2",
            "points": points((9, 9, 9, 9)),
        })
        snapshot = json.loads(self.connection.execute(
            "SELECT input_snapshot_json FROM dispatch_plans WHERE plan_id='plan-1'"
        ).fetchone()[0])
        self.assertEqual(snapshot["resources"]["pv-1"]["version"], "v1")

    def test_generation_picks_latest_version_unless_pinned(self) -> None:
        self.seed_world()
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": "v2",
            "points": points((0, 0, 0, 0)),
        })
        plan = self.service.generate_plan("plan", {
            "plan_id": "plan-latest",
            "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
            "resources": [{"id": "pv-1"}], "storages": [], "line": {"id": "line-1"}, "customers": [],
        })
        detail = self.service.plan_detail("audit", "plan-latest")
        self.assertEqual(detail["slots"][0]["generation_kw"], "0.000")
        pinned = self.service.generate_plan("plan", {
            "plan_id": "plan-pinned",
            "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
            "resources": [{"id": "pv-1", "version": "v1"}],
            "storages": [], "line": {"id": "line-1"}, "customers": [],
        })
        detail = self.service.plan_detail("audit", "plan-pinned")
        self.assertEqual(detail["slots"][0]["generation_kw"], "0.000")
        self.assertEqual(detail["slots"][3]["generation_kw"], "800.000")

    def test_confirm_locks_resources_and_conflicts_with_second_plan(self) -> None:
        self.seed_world()
        self.make_plan()
        confirmed = self.service.confirm_plan("ops", "plan-1", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        slot_rows = self.connection.execute(
            "SELECT state FROM plan_slots WHERE plan_id='plan-1'"
        ).fetchall()
        self.assertTrue(all(row["state"] == "locked" for row in slot_rows))
        # 第二份方案在同一时段锁定同一储能必须冲突（复用其余输入，仅新增预测版本）。
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": "v1b",
            "points": points((0, 50, 200, 800)),
        })
        self.service.generate_plan("plan", {
            "plan_id": "plan-2",
            "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
            "resources": [{"id": "pv-1", "version": "v1b"}],
            "storages": [{"id": "ess-1"}],
            "line": {"id": "line-1", "version": "c1"},
            "customers": [{"id": "ind-1", "version": "g1"}],
        })
        with self.assertRaises(Conflict):
            self.service.confirm_plan("ops", "plan-2", 1)

    def test_role_separation(self) -> None:
        self.seed_world()
        with self.assertRaises(Forbidden):
            self.service.generate_plan("audit", {
                "plan_id": "p-x", "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
                "resources": [{"id": "pv-1"}], "line": {"id": "line-1"},
            })
        self.make_plan()
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("plan", "plan-1", 1)

    def test_non_slot_aligned_horizon_rejected(self) -> None:
        self.seed_world()
        with self.assertRaises(ValidationFailed):
            self.service.generate_plan("plan", {
                "plan_id": "p-bad",
                "horizon_start": "2026-09-29T00:05:00Z",
                "horizon_end": "2026-09-29T01:00:00Z",
                "resources": [{"id": "pv-1"}], "line": {"id": "line-1"},
            })


class InstructionReceiptTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_world()
        self.make_plan()
        self.service.confirm_plan("ops", "plan-1", 1)
        issued = []
        for seq, payload in enumerate((
            {"resource_type": "storage", "resource_id": "ess-1", "action": "discharge", "power_kw": "200"},
            {"resource_type": "line", "resource_id": "line-1", "action": "import", "power_kw": "100"},
            {"resource_type": "customer", "resource_id": "ind-1", "action": "load", "power_kw": "300"},
        ), start=1):
            issued.append(self.service.issue_instruction("ops", {
                "plan_id": "plan-1", "slot_start": START,
                "idempotency_key": f"issue-{seq}", **payload,
            }))
        self.instructions = issued

    def test_seq_numbers_are_monotonic_and_idempotent(self) -> None:
        self.assertEqual([item["seq_no"] for item in self.instructions], [1, 2, 3])
        replay = self.service.issue_instruction("ops", {
            "plan_id": "plan-1", "slot_start": START,
            "idempotency_key": "issue-1",
            "resource_type": "storage", "resource_id": "ess-1",
            "action": "discharge", "power_kw": "200",
        })
        self.assertEqual(replay["instruction_id"], self.instructions[0]["instruction_id"])
        count = self.connection.execute("SELECT count(*) FROM dispatch_instructions").fetchone()[0]
        self.assertEqual(count, 3)

    def test_instruction_cannot_exceed_plan(self) -> None:
        with self.assertRaises(Conflict):
            self.service.issue_instruction("ops", {
                "plan_id": "plan-1", "slot_start": START,
                "idempotency_key": "issue-over",
                "resource_type": "storage", "resource_id": "ess-1",
                "action": "discharge", "power_kw": "201",
            })

    def test_happy_path_marks_slot_executed(self) -> None:
        for index, instruction in enumerate(self.instructions, start=1):
            ack = self.service.acknowledge_instruction("ops", instruction["instruction_id"], index, f"ack-{index}")
            self.assertTrue(ack["accepted"])
            self.assertEqual(ack["state"], "acknowledged")
            settled = self.service.settle_instruction(
                "ops", instruction["instruction_id"], index, f"settle-{index}", "50"
            )
            self.assertTrue(settled["accepted"])
            self.assertEqual(settled["settled_kwh"], "50")
        slot = self.connection.execute(
            "SELECT state FROM plan_slots WHERE plan_id='plan-1' AND starts_at=?", (START,)
        ).fetchone()
        self.assertEqual(slot["state"], "executed")

    def test_duplicate_and_late_receipts_do_not_settle_twice(self) -> None:
        instruction = self.instructions[0]
        self.service.acknowledge_instruction("ops", instruction["instruction_id"], 1, "ack-1")
        first = self.service.settle_instruction("ops", instruction["instruction_id"], 1, "settle-1")
        duplicate = self.service.settle_instruction("ops", instruction["instruction_id"], 1, "settle-again")
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["note"], "重复结算")
        late_ack = self.service.acknowledge_instruction("ops", instruction["instruction_id"], 1, "ack-late")
        self.assertFalse(late_ack["accepted"])
        self.assertEqual(late_ack["note"], "迟到回执")
        # 首次结算结果保持不变。
        row = self.connection.execute(
            "SELECT settled_kwh FROM dispatch_instructions WHERE instruction_id=?",
            (instruction["instruction_id"],),
        ).fetchone()
        self.assertEqual(row["settled_kwh"], first["settled_kwh"])
        # 幂等键重放返回同样的拒绝结果。
        replay = self.service.settle_instruction("ops", instruction["instruction_id"], 1, "settle-again")
        self.assertEqual(replay, duplicate)

    def test_wrong_sequence_number_is_rejected_and_replayable(self) -> None:
        instruction = self.instructions[1]
        with self.assertRaises(Conflict):
            self.service.acknowledge_instruction("ops", instruction["instruction_id"], 999, "ack-bad")
        with self.assertRaises(Conflict):
            self.service.acknowledge_instruction("ops", instruction["instruction_id"], 999, "ack-bad")
        # 正确序号仍可确认。
        ack = self.service.acknowledge_instruction("ops", instruction["instruction_id"], 2, "ack-good")
        self.assertTrue(ack["accepted"])

    def test_settlement_before_ack_rejected(self) -> None:
        instruction = self.instructions[2]
        with self.assertRaises(InvalidState):
            self.service.settle_instruction("ops", instruction["instruction_id"], 3, "settle-early")
        self.service.acknowledge_instruction("ops", instruction["instruction_id"], 3, "ack-3")
        settled = self.service.settle_instruction("ops", instruction["instruction_id"], 3, "settle-3")
        self.assertTrue(settled["accepted"])


class RevisionTests(ServiceTestBase):
    def _execute_first_slot(self) -> list[int]:
        instruction_ids = []
        for seq, payload in enumerate((
            {"resource_type": "storage", "resource_id": "ess-1", "action": "discharge", "power_kw": "200"},
            {"resource_type": "line", "resource_id": "line-1", "action": "import", "power_kw": "100"},
            {"resource_type": "customer", "resource_id": "ind-1", "action": "load", "power_kw": "300"},
        ), start=1):
            instruction = self.service.issue_instruction("ops", {
                "plan_id": "plan-1", "slot_start": START,
                "idempotency_key": f"issue-{seq}", **payload,
            })
            instruction_ids.append(instruction["instruction_id"])
            self.service.acknowledge_instruction("ops", instruction["instruction_id"], seq, f"ack-{seq}")
            self.service.settle_instruction("ops", instruction["instruction_id"], seq, f"settle-{seq}")
        return instruction_ids

    def test_revision_keeps_executed_slots_and_recalculates_rest(self) -> None:
        self.seed_world()
        self.make_plan()
        self.service.confirm_plan("ops", "plan-1", 1)
        self._execute_first_slot()
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": "v2",
            "points": points((0, 50, 800, 900)),
        })
        revised = self.service.revise_plan("plan", "plan-1")
        child_id = revised["plan_id"]
        self.assertEqual(revised["freeze_before"], "2026-09-29T00:15:00Z")
        self.assertEqual(revised["revision"], 2)

        parent = self.service.plan_detail("audit", "plan-1")
        child = self.service.plan_detail("audit", child_id)
        self.assertEqual(parent["state"], "superseded")
        # 已执行时段原样承接。
        self.assertEqual(child["slots"][0]["state"], "executed")
        self.assertEqual(child["slots"][0]["storage_discharge_kw"], "200.000")
        self.assertEqual(child["slots"][0]["import_kw"], "100.000")
        # 后续时段按 v2 重算：00:30 大发，充电+上浮+外送。
        slot_0030 = child["slots"][2]
        self.assertEqual(slot_0030["state"], "pending")
        self.assertEqual(slot_0030["storage_charge_kw"], "200.000")
        self.assertEqual(slot_0030["adjustable_kw"], "200.000")
        self.assertEqual(slot_0030["export_kw"], "100.000")
        # 修订快照引用 v2，且可再确认并锁定。
        snapshot = json.loads(self.connection.execute(
            "SELECT input_snapshot_json FROM dispatch_plans WHERE plan_id=?", (child_id,)
        ).fetchone()[0])
        self.assertEqual(snapshot["resources"]["pv-1"]["version"], "v2")
        self.assertEqual(snapshot["freeze_before"], "2026-09-29T00:15:00Z")
        confirm = self.service.confirm_plan("ops", child_id, 2)
        self.assertEqual(confirm["state"], "confirmed")
        # 旧计划未执行时段的锁已释放，可由新计划重新锁定。
        locks = self.connection.execute(
            "SELECT count(*) FROM resource_locks WHERE plan_id=? AND starts_at>=?",
            (child_id, "2026-09-29T00:15:00Z"),
        ).fetchone()[0]
        self.assertGreater(locks, 0)

    def test_revision_before_execution_recalculates_whole_horizon(self) -> None:
        self.seed_world()
        self.make_plan()
        self.service.confirm_plan("ops", "plan-1", 1)
        self.service.record_forecast("plan", {
            "resource_id": "pv-1", "kind": "solar", "version": "v2",
            "points": points((0, 0, 0, 0)),
        })
        revised = self.service.revise_plan("plan", "plan-1")
        self.assertEqual(revised["freeze_before"], START)
        child = self.service.plan_detail("audit", revised["plan_id"])
        self.assertTrue(all(slot["state"] == "pending" for slot in child["slots"]))

    def test_revision_requires_confirmed_plan(self) -> None:
        self.seed_world()
        self.make_plan()
        with self.assertRaises(InvalidState):
            self.service.revise_plan("plan", "plan-1")


class RestartTests(ServiceTestBase):
    def test_pending_plan_recovers_from_disk(self) -> None:
        self.seed_world()
        self.make_plan()
        self.service.confirm_plan("ops", "plan-1", 1)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "dispatch.sqlite3"
            disk = connect(db_path)
            disk_service = JointDispatchService(disk, self.clock)
            for user_id, role in (("plan", "planner"), ("ops", "operator"), ("audit", "auditor")):
                disk_service.create_user(user_id, user_id, role)
            disk_service.register_storage_asset("plan", {
                "storage_id": "ess-1", "name": "储能一号",
                "rated_charge_kw": "200", "rated_discharge_kw": "200",
                "capacity_kwh": "400", "efficiency_percent": "100",
            })
            disk_service.register_line_asset("plan", {"line_id": "line-1", "name": "通道"})
            disk_service.record_forecast("plan", {
                "resource_id": "pv-1", "kind": "solar", "version": "v1", "points": points((0, 50, 200, 800)),
            })
            disk_service.record_storage_availability("plan", {
                "storage_id": "ess-1", "version": "w1",
                "window_start": START, "window_end": "2026-09-29T01:00:00Z",
                "initial_soc_kwh": "100", "maintenance": [],
            })
            disk_service.record_line_capability("plan", {
                "line_id": "line-1", "version": "c1",
                "points": points((300, 300, 300, 300)), "reverse_limit_kw": "100", "maintenance": [],
            })
            disk_service.record_commitment("plan", {
                "customer_id": "ind-1", "version": "g1",
                "points": commitment_rows((300,) * 4, (500,) * 4),
            })
            disk_service.generate_plan("plan", {
                "plan_id": "plan-disk",
                "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
                "resources": [{"id": "pv-1"}], "storages": [{"id": "ess-1"}],
                "line": {"id": "line-1"}, "customers": [{"id": "ind-1"}],
            })
            disk_service.confirm_plan("ops", "plan-disk", 1)
            disk_service.issue_instruction("ops", {
                "plan_id": "plan-disk", "slot_start": START, "idempotency_key": "issue-1",
                "resource_type": "storage", "resource_id": "ess-1",
                "action": "discharge", "power_kw": "200",
            })
            disk.close()

            recovered_connection = connect(db_path)
            recovered = JointDispatchService(recovered_connection, self.clock)
            pending = recovered.pending_work("ops")
            plan = next(item for item in pending["confirmed_plans"] if item["plan_id"] == "plan-disk")
            self.assertEqual(len(plan["pending_slots"]), 4)
            self.assertEqual(plan["open_instructions"][0]["state"], "issued")
            first_slot = plan["pending_slots"][0]
            self.assertIn("line_forward_margin_kw", first_slot)
            self.assertIn("unmet_kw", first_slot)
            self.assertIn("reasons", first_slot)
            # 重启后继续闭环指令。
            ack = recovered.acknowledge_instruction("ops", 1, 1, "ack-restart")
            self.assertTrue(ack["accepted"])
            settled = recovered.settle_instruction("ops", 1, 1, "settle-restart")
            self.assertTrue(settled["accepted"])
            recovered_connection.close()


class ApiTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = JsonApplication(self.service)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        forecast = json.dumps({
            "resource_id": "pv-1", "kind": "solar", "version": "v1",
            "points": points((0, 50, 200, 800)),
        }).encode()
        response = self.app.handle("POST", "/forecasts", {"x-actor-id": "plan"}, forecast)
        self.assertEqual(response.status, 201)
        self.app.handle("POST", "/storage/availability", {"x-actor-id": "plan"}, json.dumps({
            "storage_id": "ess-1", "version": "w1",
            "window_start": START, "window_end": "2026-09-29T01:00:00Z",
            "initial_soc_kwh": "100", "maintenance": [],
        }).encode())
        self.app.handle("POST", "/line/capability", {"x-actor-id": "plan"}, json.dumps({
            "line_id": "line-1", "version": "c1",
            "points": points((300, 300, 300, 300)), "reverse_limit_kw": "100",
        }).encode())
        self.app.handle("POST", "/commitments", {"x-actor-id": "plan"}, json.dumps({
            "customer_id": "ind-1", "version": "g1",
            "points": commitment_rows((300,) * 4, (500,) * 4),
        }).encode())
        plan_body = json.dumps({
            "plan_id": "plan-api",
            "horizon_start": START, "horizon_end": "2026-09-29T01:00:00Z",
            "resources": [{"id": "pv-1"}], "storages": [{"id": "ess-1"}],
            "line": {"id": "line-1"}, "customers": [{"id": "ind-1"}],
        }).encode()
        created = self.app.handle("POST", "/plans", {"x-actor-id": "plan"}, plan_body)
        self.assertEqual(created.status, 201)
        confirmed = self.app.handle(
            "POST", "/plans/plan-api/confirm", {"x-actor-id": "ops"},
            json.dumps({"expected_revision": 1}).encode(),
        )
        self.assertEqual(confirmed.body["state"], "confirmed")
        pending = self.app.handle("GET", "/plans/pending", {"x-actor-id": "ops"})
        self.assertEqual(pending.body["confirmed_plans"][0]["plan_id"], "plan-api")

    def test_missing_actor_and_forbidden(self) -> None:
        response = self.app.handle("POST", "/plans", body=b"{}")
        self.assertEqual(response.status, 422)
        response = self.app.handle(
            "POST", "/plans", {"x-actor-id": "audit"},
            json.dumps({"plan_id": "p", "horizon_start": START, "horizon_end": START}).encode(),
        )
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
