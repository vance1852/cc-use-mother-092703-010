"""确定性分时计划算法的单元测试。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from renewable_dispatch.planning import (
    LineSpec,
    LoadSpec,
    PlanInput,
    StorageSpec,
    build_plan,
    digest,
    replay_snapshot,
)


def make_input(**overrides) -> PlanInput:
    defaults = dict(
        horizon_id="h1",
        interval_minutes=60,
        slot_count=3,
        starts_at="2026-09-29T00:00:00Z",
        generation={"wind:w1": (Decimal("0"), Decimal("40"), Decimal("0"))},
        storage=(),
        lines=(),
        loads=(LoadSpec(
            load_id="l1",
            firm_mw=(Decimal("20"),) * 3,
            curtailable_mw=(Decimal("0"),) * 3,
            reductions=(None, None, None),
        ),),
    )
    defaults.update(overrides)
    return PlanInput(**defaults)


class PlanningTests(unittest.TestCase):
    def test_storage_discharges_into_shortfall(self) -> None:
        storage = StorageSpec(
            storage_id="b1", capacity_mwh=Decimal("20"),
            max_charge_mw=Decimal("10"), max_discharge_mw=Decimal("10"),
            charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"),
            initial_soc_mwh=Decimal("20"),
            available=(True, True, True),
        )
        plan = build_plan(make_input(storage=(storage,)))
        periods = plan["periods"]
        # 槽位 0：发电 0、负荷 20，储能按额定 10MW 放电，剩余 10MW 需线路受入。
        self.assertEqual(periods[0]["storage_discharge_mw"], "10.000")
        self.assertEqual(periods[0]["unmet_demand_mw"], "10.000")
        self.assertIn("storage_discharging", periods[0]["adjustment_reasons"])
        # 槽位 1：发电 40、负荷 20，富余 20 充电，受额定 10MW 限制只充 10MW。
        self.assertEqual(periods[1]["storage_charge_mw"], "10.000")
        self.assertEqual(periods[1]["net_balance_mw"], "10.000")

    def test_storage_respects_soc_and_window(self) -> None:
        storage = StorageSpec(
            storage_id="b1", capacity_mwh=Decimal("5"),
            max_charge_mw=Decimal("10"), max_discharge_mw=Decimal("10"),
            charge_efficiency=Decimal("1"), discharge_efficiency=Decimal("1"),
            initial_soc_mwh=Decimal("0"),
            available=(False, True, True),  # 首槽检修不可用
        )
        plan = build_plan(make_input(storage=(storage,)))
        periods = plan["periods"]
        # 首槽窗口关闭，储能不动作，只能靠线路受入（无线路时全部为未满足）。
        self.assertEqual(periods[0]["storage_discharge_mw"], "0.000")
        self.assertEqual(periods[0]["unmet_demand_mw"], "20.000")
        self.assertIn("storage:b1:unavailable_window", periods[0]["adjustment_reasons"])
        # 第二槽发电富余，受 5MWh 容量限制只能充 5MW。
        self.assertEqual(periods[1]["storage_charge_mw"], "5.000")
        detail = next(item for item in periods[1]["storage"] if item["storage_id"] == "b1")
        self.assertEqual(detail["soc_mwh"], "5.000")

    def test_line_capacity_creates_unmet_and_margin(self) -> None:
        line = LineSpec(
            line_id="line-1", rated_mw=Decimal("100"),
            limits_mw=(Decimal("10"), None, None),
        )
        plan = build_plan(make_input(lines=(line,)))
        slot0 = plan["periods"][0]
        # 缺口 20，线路只能受入 10，未满足 10，余量为负。
        self.assertEqual(slot0["unmet_demand_mw"], "10.000")
        margin = slot0["line_margins"][0]
        self.assertEqual(margin["margin_mw"], "-10.000")
        self.assertTrue(margin["binding"])
        self.assertIn("line:line-1:over_capacity", slot0["adjustment_reasons"])
        self.assertFalse(plan["feasible"])

    def test_commitment_relaxation_reduces_target(self) -> None:
        loads = (LoadSpec(
            load_id="l1", firm_mw=(Decimal("20"),) * 3,
            curtailable_mw=(Decimal("8"),) * 3,
            reductions=(Decimal("5"), None, None),
        ),)
        plan = build_plan(make_input(loads=loads, generation={"wind:w1": (Decimal("0"), Decimal("0"), Decimal("0"))}))
        # 保供目标由 20 降到 15。
        self.assertEqual(plan["periods"][0]["target_load_mw"], "15.000")
        self.assertIn("load_commitment_relaxed", plan["periods"][0]["adjustment_reasons"])

    def test_deterministic_and_replayable_from_snapshot(self) -> None:
        plan1 = build_plan(make_input())
        plan2 = build_plan(make_input())
        self.assertEqual(digest(plan1), digest(plan2))
        snapshot = make_input().as_snapshot()
        replayed = replay_snapshot(snapshot)
        self.assertEqual(digest(replayed), digest(plan1))

    def test_slot_starts_align_to_grid(self) -> None:
        plan = build_plan(make_input())
        self.assertEqual(plan["periods"][2]["starts_at"], "2026-09-29T02:00:00Z")


if __name__ == "__main__":
    unittest.main()
