"""贯通版本预测、方案确认锁定、修订重算与指令结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict
from .service import JointDispatchService


HORIZON_START = "2026-09-29T00:00:00Z"


def solar_points(values: tuple[int, ...]) -> list[dict[str, object]]:
    return [
        {"starts_at": f"2026-09-29T00:{15 * index:02d}:00Z", "power_kw": value}
        for index, value in enumerate(values)
    ]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = JointDispatchService(connection, FrozenClock(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("ops", "operator"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)

    # 资源台账：一台储能、一条外送线路。
    service.register_storage_asset("plan", {
        "storage_id": "ess-1", "name": "衡山储能电站",
        "rated_charge_kw": "200", "rated_discharge_kw": "200",
        "capacity_kwh": "400", "efficiency_percent": "100",
    })
    service.register_line_asset("plan", {"line_id": "line-hy-1", "name": "衡阳北 220kV 通道"})

    # 带版本的输入：发电预测 v1、储能窗口、线路能力、用户保供承诺。
    service.record_forecast("plan", {
        "resource_id": "pv-1", "kind": "solar", "version": "v1",
        "points": solar_points((0, 50, 200, 800)),
    })
    service.record_storage_availability("plan", {
        "storage_id": "ess-1", "version": "w1",
        "rated_charge_kw": "200", "rated_discharge_kw": "200",
        "capacity_kwh": "400", "efficiency_percent": "100",
        "initial_soc_kwh": "100",
        "window_start": "2026-09-29T00:00:00Z", "window_end": "2026-09-29T01:00:00Z",
        "maintenance": [],
    })
    service.record_line_capability("plan", {
        "line_id": "line-hy-1", "version": "c1",
        "points": solar_points((300, 300, 300, 300)),
        "reverse_limit_kw": "100",
        "maintenance": [],
    })
    service.record_commitment("plan", {
        "customer_id": "ind-1", "version": "g1",
        "points": [
            {"starts_at": f"2026-09-29T00:{15 * index:02d}:00Z",
             "guaranteed_kw": "300", "max_demand_kw": "500", "curtailable_kw": "0"}
            for index in range(4)
        ],
    })

    plan = service.generate_plan("plan", {
        "plan_id": "plan-20260929",
        "horizon_start": HORIZON_START, "horizon_end": "2026-09-29T01:00:00Z",
        "resources": [{"id": "pv-1"}],
        "storages": [{"id": "ess-1"}],
        "line": {"id": "line-hy-1"},
        "customers": [{"id": "ind-1"}],
    })
    detail = service.plan_detail("plan", "plan-20260929")
    first_slot = detail["slots"][0]
    assert first_slot["storage_discharge_kw"] == "200.000"
    assert first_slot["import_kw"] == "100.000"
    assert first_slot["unmet_kw"] == "0.000"

    confirmed = service.confirm_plan("ops", "plan-20260929", 1)
    assert confirmed["state"] == "confirmed"

    # 第一个时段下发三条指令：储能放电、线路受入、用户负荷。
    instructions = []
    for seq, payload in enumerate((
        {"resource_type": "storage", "resource_id": "ess-1", "action": "discharge", "power_kw": "200"},
        {"resource_type": "line", "resource_id": "line-hy-1", "action": "import", "power_kw": "100"},
        {"resource_type": "customer", "resource_id": "ind-1", "action": "load", "power_kw": "300"},
    ), start=1):
        instruction = service.issue_instruction("ops", {
            "plan_id": "plan-20260929", "slot_start": HORIZON_START,
            "idempotency_key": f"issue-{seq}", **payload,
        })
        assert instruction["seq_no"] == seq
        instructions.append(instruction)

    for index, instruction in enumerate(instructions, start=1):
        service.acknowledge_instruction("ops", instruction["instruction_id"], instruction["seq_no"], f"ack-{index}")
        service.settle_instruction("ops", instruction["instruction_id"], instruction["seq_no"], f"settle-{index}")

    executed = service.plan_detail("audit", "plan-20260929")
    assert executed["slots"][0]["state"] == "executed"

    # 重复结算回执：不得二次结算。
    duplicate = service.settle_instruction(
        "ops", instructions[0]["instruction_id"], instructions[0]["seq_no"], "settle-duplicate"
    )
    assert duplicate["accepted"] is False and duplicate["note"] == "重复结算"
    # 事件序号对不上：拒绝。
    try:
        service.acknowledge_instruction("ops", instructions[1]["instruction_id"], 999, "ack-wrong-seq")
        raise AssertionError("序号错误的回执必须被拒绝")
    except Conflict:
        pass

    # 预测修订：只重算未执行时段，首个时段原样承接。
    service.record_forecast("plan", {
        "resource_id": "pv-1", "kind": "solar", "version": "v2",
        "points": solar_points((0, 50, 800, 900)),
    })
    revised = service.revise_plan("plan", "plan-20260929")
    child_id = revised["plan_id"]
    assert revised["freeze_before"] == "2026-09-29T00:15:00Z"
    child = service.plan_detail("audit", child_id)
    assert child["slots"][0]["state"] == "executed"
    assert child["slots"][0]["storage_discharge_kw"] == "200.000"
    assert child["slots"][2]["state"] == "pending"
    # v2 在 00:30 大发：富余先充电，再上浮可调负荷，最后外送。
    slot_0030 = child["slots"][2]
    assert slot_0030["storage_charge_kw"] == "200.000"
    assert slot_0030["adjustable_kw"] == "200.000"
    assert slot_0030["export_kw"] == "100.000"

    service.confirm_plan("ops", child_id, 2)

    # 重启恢复：新服务实例从同一数据库恢复待执行计划。
    restarted = JointDispatchService(connection, FrozenClock(datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)))
    pending = restarted.pending_work("ops")
    pending_plan = next(item for item in pending["confirmed_plans"] if item["plan_id"] == child_id)
    assert [slot["starts_at"] for slot in pending_plan["pending_slots"]] == [
        "2026-09-29T00:15:00Z", "2026-09-29T00:30:00Z", "2026-09-29T00:45:00Z",
    ]
    margins = pending_plan["pending_slots"][-1]
    assert "line_forward_margin_kw" in margins and "unmet_kw" in margins and margins["reasons"]

    audit = service.audit_chain("audit")
    result = {
        "status": "ok",
        "plan_id": "plan-20260929",
        "revised_plan_id": child_id,
        "confirmed_locks": confirmed["locks"],
        "executed_instructions": len(instructions),
        "duplicate_receipt": duplicate["note"],
        "summary": child["summary"],
        "fulfillment_percent": child["summary"]["guarantee_fulfillment_percent"],
        "pending_slots": len(pending_plan["pending_slots"]),
        "input_sha256": plan["input_sha256"],
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行新能源联合消纳服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
