"""联合消纳计划服务的事务与业务规则测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from renewable_dispatch.clock import FrozenClock
from renewable_dispatch.errors import Conflict, Forbidden, InvalidState
from renewable_dispatch.service import DispatchService


def horizon(n: int = 4) -> dict:
    return {
        "horizon_id": "h1",
        "starts_at": "2026-09-29T00:00:00Z",
        "interval_minutes": 60,
        "slot_count": n,
    }


def forecast(forecast_id: str, version: int, power: list[str]) -> dict:
    return {
        "forecast_id": forecast_id, "resource_id": "wind-1", "resource_kind": "wind",
        "version": version, "interval_minutes": 60, "power_mw": power,
        "observed_at": "2026-09-28T20:00:00Z",
    }


def register_baseline(service: DispatchService, n: int = 4, power=None) -> None:
    power = power or ["0", "30", "30", "0"][:n] if n <= 4 else ["0", "30", "30", "0"] + ["0"] * (n - 4)
    service.create_horizon("plan", horizon(n))
    service.record_forecast("plan", forecast("f-v1", 1, power))
    service.register_storage("plan", {
        "storage_id": "bess", "storage_kind": "battery", "interval_minutes": 60,
        "capacity_mwh": "20", "max_charge_mw": "10", "max_discharge_mw": "10",
        "charge_efficiency": "1", "discharge_efficiency": "1",
    })
    service.record_storage_window("plan", {
        "storage_id": "bess", "version": 1, "interval_minutes": 60,
        "initial_soc_mwh": "10", "available": [True] * n, "reason": "正常",
    })
    service.register_line("plan", {"line_id": "line", "interval_minutes": 60, "rated_mw": "50"})
    service.record_line_capability("plan", {
        "line_id": "line", "version": 1, "interval_minutes": 60, "rated_mw": "50",
        "limits_mw": [None] * n, "reason": "正常",
    })
    service.register_load("plan", {"load_id": "plant", "load_kind": "industrial", "interval_minutes": 60})
    service.record_commitment("plan", {
        "load_id": "plant", "version": 1, "interval_minutes": 60,
        "firm_mw": ["25"] * n, "curtailable_mw": ["0"] * n,
        "reductions": [None] * n, "reason": "基线",
    })


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc))
        self.service = DispatchService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("dispatch", "dispatcher"),
            ("operator", "operator"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        register_baseline(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_generate_confirm_and_snapshot_replay(self) -> None:
        draft = self.service.generate_plan("plan", "p1", "h1")
        self.assertEqual(draft["revision"], 1)
        self.assertEqual(len(draft["snapshot_sha256"]), 64)
        confirmation = self.service.confirm_plan("dispatch", "p1", 1)
        self.assertEqual({item["resource_id"] for item in confirmation["locked_resources"]},
                         {"bess", "line", "plant"})
        locks = self.connection.execute("SELECT count(*) FROM resource_locks").fetchone()[0]
        self.assertEqual(locks, 3)
        verify = self.service.verify_snapshot("audit", "p1", 1)
        self.assertTrue(verify["replay_matches"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.generate_plan("operator", "p1", "h1")
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("plan", "p1", 1)
        with self.assertRaises(Forbidden):
            self.service.record_forecast("dispatch", forecast("f-x", 2, ["0"] * 4))

    def test_forecast_revision_only_recomputes_unexecuted_slots(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        # 推进两个小时：槽位 0、1 已执行。
        self.clock.advance(hours=4)
        self.service.record_forecast("plan", forecast("f-v2", 2, ["0", "30", "5", "0"]))
        draft2 = self.service.generate_plan("plan", "p1", "h1", note="修订")
        self.assertEqual(draft2["revision"], 2)
        self.assertEqual(draft2["frozen_slots"], 2)
        frozen = [p for p in draft2["periods"] if p["slot"] < 2]
        changed = [p for p in draft2["periods"] if p["slot"] >= 2]
        self.assertTrue(all(p["frozen"] for p in frozen))
        self.assertTrue(all(not p["frozen"] for p in changed))
        # 已冻结槽位不随后续预测改变：槽位 1 仍按 v1 的 30MW 计。
        self.assertEqual(frozen[1]["generation_mw"], "30.000")
        # 未执行槽位 2 按 v2 的 5MW 计，出现保供缺口。
        slot2 = next(p for p in changed if p["slot"] == 2)
        self.assertEqual(slot2["generation_mw"], "5.000")

    def test_regenerating_with_unchanged_inputs_is_conflict(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        self.clock.advance(hours=2)
        with self.assertRaises(Conflict):
            self.service.generate_plan("plan", "p1", "h1")

    def test_confirm_revision_supersedes_old_and_moves_locks(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        self.clock.advance(hours=2)
        self.service.record_forecast("plan", forecast("f-v2", 2, ["0", "30", "10", "0"]))
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 2)
        old = self.service.plan_view("audit", "p1", 1)
        self.assertEqual(old["state"], "superseded")
        # 锁全部归属新版本。
        owners = {row["revision"] for row in self.connection.execute(
            "SELECT DISTINCT revision FROM resource_locks")}
        self.assertEqual(owners, {2})
        # 旧版本未执行指令被取消。
        cancelled = self.connection.execute(
            "SELECT count(*) FROM dispatch_instructions WHERE revision=1 AND state='cancelled'"
        ).fetchone()[0]
        self.assertGreater(cancelled, 0)

    def test_resource_cannot_be_locked_by_two_plans(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        self.service.generate_plan("plan", "p2", "h1")
        with self.assertRaises(Conflict):
            self.service.confirm_plan("dispatch", "p2", 1)

    def test_duplicate_and_late_receipts_do_not_settle_twice(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        confirmation = self.service.confirm_plan("dispatch", "p1", 1)
        seq = confirmation["instructions"][0]["event_seq"]
        energy = confirmation["instructions"][0]["energy_mwh"]
        # 时段未结束时结算回执过早，记迟到。
        early = self.service.record_receipt("operator", {
            "plan_id": "p1", "revision": 1, "event_seq": seq,
            "phase": "settled", "settled_energy_mwh": energy, "idempotency_key": "k-early",
        })
        self.assertEqual(early["outcome"], "late")
        # 推进到槽位 0 结束（01:00Z）之后，结算正常受理。
        self.clock.advance(hours=3, minutes=1)
        first = self.service.record_receipt("operator", {
            "plan_id": "p1", "revision": 1, "event_seq": seq,
            "phase": "settled", "settled_energy_mwh": energy, "idempotency_key": "k-1",
        })
        self.assertEqual(first["outcome"], "accepted")
        self.assertTrue(first["settled"])
        # 相同幂等键回放。
        replay = self.service.record_receipt("operator", {
            "plan_id": "p1", "revision": 1, "event_seq": seq,
            "phase": "settled", "settled_energy_mwh": energy, "idempotency_key": "k-1",
        })
        self.assertEqual(replay, first)
        # 换键二次结算 -> 迟到，不重复结算。
        second = self.service.record_receipt("operator", {
            "plan_id": "p1", "revision": 1, "event_seq": seq,
            "phase": "settled", "settled_energy_mwh": energy, "idempotency_key": "k-2",
        })
        self.assertEqual(second["outcome"], "late")
        self.assertFalse(second["settled"])
        settled_count = self.connection.execute(
            "SELECT count(*) FROM dispatch_instructions WHERE state='settled'"
        ).fetchone()[0]
        self.assertEqual(settled_count, 1)

    def test_same_idempotency_key_different_body_conflicts(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        confirmation = self.service.confirm_plan("dispatch", "p1", 1)
        seq = confirmation["instructions"][0]["event_seq"]
        self.clock.advance(hours=3, minutes=1)
        payload = {
            "plan_id": "p1", "revision": 1, "event_seq": seq,
            "phase": "settled", "settled_energy_mwh": "5", "idempotency_key": "k",
        }
        self.service.record_receipt("operator", payload)
        with self.assertRaises(Conflict):
            self.service.record_receipt("operator", {**payload, "settled_energy_mwh": "9"})

    def test_operations_view_reports_margins_unmet_and_reasons(self) -> None:
        self.service.record_line_capability("plan", {
            "line_id": "line", "version": 2, "interval_minutes": 60, "rated_mw": "50",
            "limits_mw": ["5", None, None, None], "reason": "首小时检修",
        })
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        view = self.service.operations_view("dispatch", "h1")
        slot0 = view["periods"][0]
        self.assertIn("unmet_demand_mw", slot0)
        self.assertTrue(any(m["binding"] for m in slot0["line_margins"]))
        self.assertTrue(slot0["adjustment_reasons"])
        self.assertEqual(view["state"], "confirmed")

    def test_recover_pending_after_restart(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        self.service.confirm_plan("dispatch", "p1", 1)
        restarted = DispatchService(self.connection, self.clock)
        recovered = restarted.recover_pending()
        plan = next(item for item in recovered["plans"] if item["plan_id"] == "p1")
        self.assertEqual(plan["revision"], 1)
        self.assertTrue(plan["pending_instructions"])

    def test_persistence_across_file_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispatch.sqlite3"
            connection = None
            from renewable_dispatch.storage import connect
            connection = connect(path)
            service = DispatchService(connection, self.clock)
            for user_id, role in (
                ("plan", "planner"), ("dispatch", "dispatcher"),
                ("operator", "operator"), ("audit", "auditor"),
            ):
                service.create_user(user_id, user_id, role)
            register_baseline(service)
            service.generate_plan("plan", "p1", "h1")
            service.confirm_plan("dispatch", "p1", 1)
            connection.close()

            connection2 = connect(path)
            service2 = DispatchService(connection2, self.clock)
            recovered = service2.recover_pending()
            self.assertTrue(any(p["plan_id"] == "p1" for p in recovered["plans"]))
            connection2.close()

    def test_cannot_generate_without_forecast(self) -> None:
        self.connection.execute("UPDATE generation_forecasts SET state='retired'")
        with self.assertRaises(InvalidState):
            self.service.generate_plan("plan", "px", "h1")

    def test_audit_chain_is_valid(self) -> None:
        self.service.generate_plan("plan", "p1", "h1")
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)


if __name__ == "__main__":
    unittest.main()
