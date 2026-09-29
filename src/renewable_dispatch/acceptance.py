"""贯通版本化输入、分时方案、锁定、重算、回执结算与重启恢复的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import DispatchService
from .storage import inspect_schema


def _build_world(service: DispatchService) -> None:
    for user_id, role in (
        ("plan", "planner"),
        ("dispatch", "dispatcher"),
        ("operator", "operator"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.create_horizon("plan", {
        "horizon_id": "day-0929",
        "starts_at": "2026-09-29T00:00:00Z",
        "interval_minutes": 60,
        "slot_count": 6,
    })

    # 风电预测 v1：前 3 小时无风，中午起风。
    wind_v1 = ["0", "0", "0", "40", "50", "30"]
    service.record_forecast("plan", {
        "forecast_id": "wind-f-v1", "resource_id": "wind-farm-north", "resource_kind": "wind",
        "version": 1, "interval_minutes": 60, "power_mw": wind_v1,
        "observed_at": "2026-09-28T20:00:00Z",
    })
    # 光伏预测 v1：白天大发。
    solar_v1 = ["0", "10", "30", "40", "20", "0"]
    service.record_forecast("plan", {
        "forecast_id": "solar-f-v1", "resource_id": "solar-park-east", "resource_kind": "solar",
        "version": 1, "interval_minutes": 60, "power_mw": solar_v1,
        "observed_at": "2026-09-28T20:00:00Z",
    })

    # 储能 20MWh，额定 10MW，首版全程可用。
    service.register_storage("plan", {
        "storage_id": "bess-1", "storage_kind": "battery", "interval_minutes": 60,
        "capacity_mwh": "20", "max_charge_mw": "10", "max_discharge_mw": "10",
        "charge_efficiency": "0.95", "discharge_efficiency": "0.95",
    })
    service.record_storage_window("plan", {
        "storage_id": "bess-1", "version": 1, "interval_minutes": 60,
        "initial_soc_mwh": "10",
        "available": [True, True, True, True, True, True],
        "reason": "正常运行",
    })

    # 外送/受电通道额定 30MW。
    service.register_line("plan", {
        "line_id": "line-main", "interval_minutes": 60, "rated_mw": "30",
    })
    service.record_line_capability("plan", {
        "line_id": "line-main", "version": 1, "interval_minutes": 60, "rated_mw": "30",
        "limits_mw": [None, None, None, None, None, None],
        "reason": "正常方式",
    })

    # 工业用户保供 25MW，另有最多 10MW 可调负荷。
    service.register_load("plan", {
        "load_id": "plant-a", "load_kind": "industrial", "interval_minutes": 60,
    })
    service.record_commitment("plan", {
        "load_id": "plant-a", "version": 1, "interval_minutes": 60,
        "firm_mw": ["25", "25", "25", "25", "25", "25"],
        "curtailable_mw": ["10", "10", "10", "10", "10", "10"],
        "reductions": [None, None, None, None, None, None],
        "reason": "基线保供承诺",
    })


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc))
    service = DispatchService(connection, clock)

    _build_world(service)

    # 首版分时方案（全部为未执行时段）。
    draft1 = service.generate_plan("plan", "plan-001", "day-0929", note="日前初版")
    assert draft1["revision"] == 1
    confirmed1 = service.confirm_plan("dispatch", "plan-001", 1)
    locked_first = confirmed1["locked_resources"]

    # 首版在执行前可以核对快照可复现。
    verify1 = service.verify_snapshot("audit", "plan-001", 1)
    assert verify1["replay_matches"] is True

    # 时间推进：前两个时段已执行（槽位 0 的放电指令按计量结算）。
    clock.current = datetime(2026, 9, 29, 2, 30, tzinfo=timezone.utc)
    instructions = confirmed1["instructions"]
    slot0_discharge = instructions[0]        # 槽位 0 放电，事件序号 1，时段已结束

    # 时段结束后才到达的“调度确认”属于迟到确认，不改变指令状态。
    late_ack = service.record_receipt("operator", {
        "plan_id": "plan-001", "revision": 1, "event_seq": slot0_discharge["event_seq"],
        "phase": "acknowledged", "idempotency_key": "rcpt-ack-late",
    })
    assert late_ack["outcome"] == "late" and late_ack["settled"] is False

    # 计量结算在时段结束后到达，正常受理。
    settle = service.record_receipt("operator", {
        "plan_id": "plan-001", "revision": 1, "event_seq": slot0_discharge["event_seq"],
        "phase": "settled", "settled_energy_mwh": slot0_discharge["energy_mwh"],
        "idempotency_key": "rcpt-1",
    })
    assert settle["outcome"] == "accepted" and settle["settled"] is True

    # 重复回执（相同幂等键）回放，不重复结算。
    duplicate = service.record_receipt("operator", {
        "plan_id": "plan-001", "revision": 1, "event_seq": slot0_discharge["event_seq"],
        "phase": "settled", "settled_energy_mwh": slot0_discharge["energy_mwh"],
        "idempotency_key": "rcpt-1",
    })
    assert duplicate["outcome"] == "accepted" and duplicate["settled"] is True

    # 换幂等键对同一事件二次结算：指令已结算，回执记为迟到，不重复结算。
    late_double = service.record_receipt("operator", {
        "plan_id": "plan-001", "revision": 1, "event_seq": slot0_discharge["event_seq"],
        "phase": "settled", "settled_energy_mwh": slot0_discharge["energy_mwh"],
        "idempotency_key": "rcpt-1-retry",
    })
    assert late_double["outcome"] == "late" and late_double["settled"] is False

    # 预测修订：风电中午下调到 20/25/15，线路第 4 小时检修降额至 10MW，
    # 用户保供在第 4 小时放宽 5MW。只重算未执行时段（槽位 2-5）。
    service.record_forecast("plan", {
        "forecast_id": "wind-f-v2", "resource_id": "wind-farm-north", "resource_kind": "wind",
        "version": 2, "interval_minutes": 60, "power_mw": ["0", "0", "0", "20", "25", "15"],
        "observed_at": "2026-09-29T02:10:00Z",
    })
    service.record_line_capability("plan", {
        "line_id": "line-main", "version": 2, "interval_minutes": 60, "rated_mw": "30",
        "limits_mw": [None, None, None, "10", None, None],
        "reason": "第 4 小时线路检修降额",
    })
    service.record_commitment("plan", {
        "load_id": "plant-a", "version": 2, "interval_minutes": 60,
        "firm_mw": ["25", "25", "25", "25", "25", "25"],
        "curtailable_mw": ["10", "10", "10", "10", "10", "10"],
        "reductions": [None, None, None, "5", None, None],
        "reason": "检修时段用户同意放宽 5MW",
    })

    draft2 = service.generate_plan("plan", "plan-001", "day-0929", note="风电修订与线路检修")
    assert draft2["revision"] == 2 and draft2["frozen_slots"] == 2
    frozen_slots = [p for p in draft2["periods"] if p["slot"] < 2]
    future_slots = [p for p in draft2["periods"] if p["slot"] >= 2]
    assert all(p["frozen"] for p in frozen_slots)
    assert all(not p["frozen"] for p in future_slots)

    confirmed2 = service.confirm_plan("dispatch", "plan-001", 2)
    # 旧版本被取代、锁转移到新版本；其未执行指令取消，已结算指令不受影响。
    old_plan = service.plan_view("audit", "plan-001", 1)
    assert old_plan["state"] == "superseded"

    verify2 = service.verify_snapshot("audit", "plan-001", 2)
    assert verify2["replay_matches"] is True

    operations = service.operations_view("dispatch", "day-0929")
    assert operations["revision"] == 2 and operations["executed_slots"] == 2
    assert len(operations["pending_instructions"]) == len(confirmed2["instructions"])
    slot3 = next(p for p in operations["periods"] if p["slot"] == 3)
    assert any("line:line-main:over_capacity" in r for r in slot3["adjustment_reasons"]) or \
           any("line:line-main:at_capacity" in r for r in slot3["adjustment_reasons"])

    # 新版本指令按事件序号继续编号（不与旧版本冲突）。
    new_discharge = next(
        item for item in confirmed2["instructions"] if item["action"] == "discharge"
    )
    assert new_discharge["event_seq"] > max(item["event_seq"] for item in instructions)
    settle_new = service.record_receipt("operator", {
        "plan_id": "plan-001", "revision": 2, "event_seq": new_discharge["event_seq"],
        "phase": "acknowledged", "idempotency_key": "rcpt-v2-ack",
    })
    assert settle_new["outcome"] == "accepted"

    # 服务重启：新服务对象指向同一内存库（线上为同一 SQLite 文件），恢复待执行计划。
    recovered_service = DispatchService(connection, clock)
    recovered = recovered_service.recover_pending()
    recovered_plan = next(item for item in recovered["plans"] if item["plan_id"] == "plan-001")
    assert recovered_plan["revision"] == 2
    assert recovered_plan["pending_instructions"], "应恢复出新版本的待执行指令"

    audit = service.audit_chain("audit")
    result = {
        "status": "ok",
        "schema": inspect_schema(connection),
        "plan_revision": draft2["revision"],
        "frozen_slots": draft2["frozen_slots"],
        "locked_resources": confirmed2["locked_resources"],
        "first_revision_total_unmet": draft1["total_unmet_demand_mw"],
        "second_revision_total_unmet": draft2["total_unmet_demand_mw"],
        "slot3_margins": slot3["line_margins"],
        "receipts": {
            "late_ack": late_ack["outcome"],
            "settle": settle["outcome"],
            "duplicate": duplicate["outcome"],
            "late_double_settle": late_double["outcome"],
            "new_ack": settle_new["outcome"],
        },
        "recovered_pending_instructions": len(recovered_plan["pending_instructions"]),
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行新能源联合消纳计划服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
