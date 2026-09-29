"""确定性的分时联合消纳计划计算。

输入统一为对齐到同一时间网格的功率序列，算法：
1. 逐时段汇总新能源预测出力与用户保供负荷（含承诺变化）；
2. 在净负荷为正时优先用储能放电、为负时按可用功率充电，受储能 SOC 与可用窗口约束；
3. 叠加输变电线路的分时能力，给出约束余量；
4. 记录未满足需求（保供缺口）与每一时段的调整原因。

结果只依赖输入快照，相同输入必产生相同输出（canonical JSON + sha256 校验）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .clock import parse_utc, utc_text


ZERO = Decimal("0")
QUANTUM = Decimal("0.001")


def q(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_default)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StorageSpec:
    storage_id: str
    capacity_mwh: Decimal
    max_charge_mw: Decimal
    max_discharge_mw: Decimal
    charge_efficiency: Decimal
    discharge_efficiency: Decimal
    initial_soc_mwh: Decimal
    available: tuple[bool, ...]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LineSpec:
    line_id: str
    rated_mw: Decimal
    limits_mw: tuple[Decimal | None, ...]
    reason: str = ""

    def limit_at(self, index: int) -> Decimal:
        if index < len(self.limits_mw) and self.limits_mw[index] is not None:
            return self.limits_mw[index]  # type: ignore[return-value]
        return self.rated_mw


@dataclass(frozen=True, slots=True)
class LoadSpec:
    load_id: str
    firm_mw: tuple[Decimal, ...]
    curtailable_mw: tuple[Decimal, ...]
    reductions: tuple[Decimal | None, ...]
    reason: str = ""


@dataclass(slots=True)
class PlanInput:
    """生成方案所用的输入快照（已是对齐后的网格）。"""

    horizon_id: str
    interval_minutes: int
    slot_count: int
    starts_at: str
    generation: Mapping[str, Sequence[Decimal]]
    storage: tuple[StorageSpec, ...] = ()
    lines: tuple[LineSpec, ...] = ()
    loads: tuple[LoadSpec, ...] = ()
    # 每个输入序列采用的版本编号（预测/窗口/能力/保供版本），随快照一并留存。
    basis: dict[str, object] = field(default_factory=dict)

    def as_snapshot(self) -> dict[str, object]:
        return {
            "horizon_id": self.horizon_id,
            "starts_at": self.starts_at,
            "interval_minutes": self.interval_minutes,
            "slot_count": self.slot_count,
            "generation": {
                key: [decimal_text(point) for point in series]
                for key, series in sorted(self.generation.items())
            },
            "storage": [
                {
                    "storage_id": item.storage_id,
                    "capacity_mwh": decimal_text(item.capacity_mwh),
                    "max_charge_mw": decimal_text(item.max_charge_mw),
                    "max_discharge_mw": decimal_text(item.max_discharge_mw),
                    "charge_efficiency": decimal_text(item.charge_efficiency),
                    "discharge_efficiency": decimal_text(item.discharge_efficiency),
                    "initial_soc_mwh": decimal_text(item.initial_soc_mwh),
                    "available": [bool(flag) for flag in item.available],
                    "reason": item.reason,
                }
                for item in sorted(self.storage, key=lambda s: s.storage_id)
            ],
            "lines": [
                {
                    "line_id": item.line_id,
                    "rated_mw": decimal_text(item.rated_mw),
                    "limits_mw": [None if value is None else decimal_text(value) for value in item.limits_mw],
                    "reason": item.reason,
                }
                for item in sorted(self.lines, key=lambda s: s.line_id)
            ],
            "loads": [
                {
                    "load_id": item.load_id,
                    "firm_mw": [decimal_text(point) for point in item.firm_mw],
                    "curtailable_mw": [decimal_text(point) for point in item.curtailable_mw],
                    "reductions": [None if value is None else decimal_text(value) for value in item.reductions],
                    "reason": item.reason,
                }
                for item in sorted(self.loads, key=lambda s: s.load_id)
            ],
        }


def build_plan(input_data: PlanInput) -> dict[str, object]:
    """根据输入快照逐时段模拟储能并计算保供缺口与约束余量。

    功率符号约定（MW）：
    balance = 新能源出力 - 储能充电 + 储能放电 - 保供目标负荷
    balance < 0 为需通过线路受入的缺口；balance > 0 为可外送的富余。
    """

    slots = input_data.slot_count
    hours = Decimal(input_data.interval_minutes) / Decimal("60")

    generation_total = [ZERO] * slots
    for series in input_data.generation.values():
        for index, point in enumerate(series):
            generation_total[index] += point
    generation_total = [q(value) for value in generation_total]

    firm_total = [ZERO] * slots
    reduction_total = [ZERO] * slots
    curtailable_total = [ZERO] * slots
    for load in input_data.loads:
        for index in range(slots):
            firm_total[index] += load.firm_mw[index]
            curtailable_total[index] += load.curtailable_mw[index]
            if load.reductions[index] is not None:
                reduction_total[index] += load.reductions[index]  # type: ignore[operator]
    # 保供目标 = 固定保供 - 已登记的承诺放宽（不低于 0）。
    target_load = [q(max(ZERO, firm_total[i] - reduction_total[i])) for i in range(slots)]

    soc = {item.storage_id: item.initial_soc_mwh for item in input_data.storage}
    periods: list[dict[str, object]] = []

    for index in range(slots):
        reasons: list[str] = []
        net = q(generation_total[index] - target_load[index])

        charge_total = ZERO
        discharge_total = ZERO
        storage_detail = []
        for item in sorted(input_data.storage, key=lambda s: s.storage_id):
            available = index < len(item.available) and item.available[index]
            charge = ZERO
            discharge = ZERO
            if available:
                if net < ZERO:
                    # 发电不足：放电补缺口，受额定功率与 SOC（含放电效率）限制。
                    deliverable_mw = soc[item.storage_id] * item.discharge_efficiency / hours
                    want = -net - discharge_total
                    discharge = q(min(max(ZERO, want), item.max_discharge_mw, max(ZERO, deliverable_mw)))
                    if discharge > ZERO:
                        soc[item.storage_id] = q(soc[item.storage_id] - discharge / item.discharge_efficiency * hours)
                        discharge_total += discharge
                elif net > ZERO:
                    # 发电富余：充电吸纳，受额定功率与剩余容量限制。
                    room = item.capacity_mwh - soc[item.storage_id]
                    headroom_mw = room / item.charge_efficiency / hours
                    want = net - charge_total
                    charge = q(min(max(ZERO, want), item.max_charge_mw, max(ZERO, headroom_mw)))
                    if charge > ZERO:
                        soc[item.storage_id] = q(soc[item.storage_id] + charge * item.charge_efficiency * hours)
                        charge_total += charge
            elif net != ZERO:
                reasons.append(f"storage:{item.storage_id}:unavailable_window")
            storage_detail.append(
                {
                    "storage_id": item.storage_id,
                    "available": available,
                    "charge_mw": decimal_text(charge),
                    "discharge_mw": decimal_text(discharge),
                    "soc_mwh": decimal_text(soc[item.storage_id]),
                }
            )

        charge_total = q(charge_total)
        discharge_total = q(discharge_total)
        balance = q(generation_total[index] - charge_total + discharge_total - target_load[index])
        need_import = q(max(ZERO, -balance))
        need_export = q(max(ZERO, balance))

        line_rows = []
        tightest_limit: Decimal | None = None
        for line in sorted(input_data.lines, key=lambda s: s.line_id):
            limit = line.limit_at(index)
            flow = q(abs(balance))
            margin = q(limit - flow)
            if margin < ZERO:
                reasons.append(f"line:{line.line_id}:over_capacity")
            elif margin == ZERO:
                reasons.append(f"line:{line.line_id}:at_capacity")
            line_rows.append(
                {
                    "line_id": line.line_id,
                    "limit_mw": decimal_text(limit),
                    "flow_mw": decimal_text(flow),
                    "margin_mw": decimal_text(margin),
                    "binding": margin <= ZERO,
                }
            )
            tightest_limit = limit if tightest_limit is None else min(tightest_limit, limit)

        # 线路受入能力不足以覆盖缺口的部分即未满足需求；外送受阻的部分为弃电。
        if tightest_limit is None:
            unmet = need_import
            curtailed = ZERO
        else:
            unmet = q(max(ZERO, need_import - tightest_limit))
            curtailed = q(max(ZERO, need_export - tightest_limit))
        if unmet > ZERO:
            reasons.append("unmet_demand")
        if curtailed > ZERO:
            reasons.append("renewable_curtailment")
        if charge_total > ZERO:
            reasons.append("storage_charging")
        if discharge_total > ZERO:
            reasons.append("storage_discharging")
        if reduction_total[index] > ZERO:
            reasons.append("load_commitment_relaxed")

        periods.append(
            {
                "slot": index,
                "starts_at": _slot_start(input_data.starts_at, index, input_data.interval_minutes),
                "generation_mw": decimal_text(generation_total[index]),
                "target_load_mw": decimal_text(target_load[index]),
                "firm_load_mw": decimal_text(q(firm_total[index])),
                "curtailable_mw": decimal_text(q(curtailable_total[index])),
                "commitment_reduction_mw": decimal_text(q(reduction_total[index])),
                "storage_charge_mw": decimal_text(charge_total),
                "storage_discharge_mw": decimal_text(discharge_total),
                "storage": storage_detail,
                "net_balance_mw": decimal_text(balance),
                "unmet_demand_mw": decimal_text(unmet),
                "curtailed_mw": decimal_text(curtailed),
                "line_margins": line_rows,
                "adjustment_reasons": reasons,
            }
        )

    total_unmet = q(sum((Decimal(period["unmet_demand_mw"]) for period in periods), ZERO))  # type: ignore[arg-type]
    total_curtailed = q(sum((Decimal(period["curtailed_mw"]) for period in periods), ZERO))  # type: ignore[arg-type]
    return {
        "horizon_id": input_data.horizon_id,
        "interval_minutes": input_data.interval_minutes,
        "slot_count": slots,
        "feasible": total_unmet == ZERO,
        "total_unmet_demand_mw": decimal_text(total_unmet),
        "total_curtailed_mw": decimal_text(total_curtailed),
        "periods": periods,
    }


def _slot_start(starts_at: str, index: int, interval_minutes: int) -> str:
    return utc_text(parse_utc(starts_at, "starts_at") + timedelta(minutes=interval_minutes * index))


def replay_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """从保存的输入快照重建方案，供审计与重算核对。"""

    slots = int(snapshot["slot_count"])  # type: ignore[arg-type]
    interval = int(snapshot["interval_minutes"])  # type: ignore[arg-type]
    generation = {
        str(key): tuple(Decimal(str(point)) for point in series)
        for key, series in snapshot.get("generation", {}).items()  # type: ignore[union-attr]
    }
    storage = tuple(
        StorageSpec(
            storage_id=item["storage_id"],
            capacity_mwh=Decimal(str(item["capacity_mwh"])),
            max_charge_mw=Decimal(str(item["max_charge_mw"])),
            max_discharge_mw=Decimal(str(item["max_discharge_mw"])),
            charge_efficiency=Decimal(str(item["charge_efficiency"])),
            discharge_efficiency=Decimal(str(item["discharge_efficiency"])),
            initial_soc_mwh=Decimal(str(item["initial_soc_mwh"])),
            available=tuple(bool(flag) for flag in item["available"]),
            reason=item.get("reason", ""),
        )
        for item in snapshot.get("storage", [])  # type: ignore[union-attr]
    )
    lines = tuple(
        LineSpec(
            line_id=item["line_id"],
            rated_mw=Decimal(str(item["rated_mw"])),
            limits_mw=tuple(None if value is None else Decimal(str(value)) for value in item["limits_mw"]),
            reason=item.get("reason", ""),
        )
        for item in snapshot.get("lines", [])  # type: ignore[union-attr]
    )
    loads = tuple(
        LoadSpec(
            load_id=item["load_id"],
            firm_mw=tuple(Decimal(str(point)) for point in item["firm_mw"]),
            curtailable_mw=tuple(Decimal(str(point)) for point in item["curtailable_mw"]),
            reductions=tuple(None if value is None else Decimal(str(value)) for value in item["reductions"]),
            reason=item.get("reason", ""),
        )
        for item in snapshot.get("loads", [])  # type: ignore[union-attr]
    )
    plan_input = PlanInput(
        horizon_id=str(snapshot["horizon_id"]),
        interval_minutes=interval,
        slot_count=slots,
        starts_at=str(snapshot["starts_at"]),
        generation=generation,
        storage=storage,
        lines=lines,
        loads=loads,
    )
    return build_plan(plan_input)
