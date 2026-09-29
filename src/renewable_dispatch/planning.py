"""确定性的分时联合消纳计算。

约定（单个园区、一条外送线路走廊）：

- 时间轴按 15 分钟切分，功率 kW、电量 kWh，统一 ``Decimal`` 量化到 0.001；
- 每个时段先由新能源直供保供负荷，不足时依次释放储能、受入外购电，
  仍不足的部分记为未满足需求；
- 富余新能源优先存入储能，再上浮工业可调负荷，再经线路外送，
  最后仍无法消纳的部分记为弃电；
- 储能 SOC 按时序递推，重算未执行时段时由服务层传入边界 SOC。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")
SLOT_HOURS = Decimal("0.25")
QUANT = Decimal("0.001")


def q(value: Decimal) -> Decimal:
    return value.quantize(QUANT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def slot_axis(start: str, end: str) -> list[str]:
    """返回 ``[start, end)`` 内对齐 15 分钟的全部时段起点。"""
    current = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc)
    finish = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc)
    if finish <= current:
        raise ValueError("方案结束时间必须晚于开始时间")
    delta = finish - current
    if delta.total_seconds() % 900 != 0:
        raise ValueError("方案时间轴必须是 15 分钟的整数倍")
    keys: list[str] = []
    while current < finish:
        keys.append(current.isoformat().replace("+00:00", "Z"))
        current += timedelta(minutes=15)
    return keys


def _within(text: str, starts_at: str, ends_at: str) -> bool:
    return starts_at <= text < ends_at


@dataclass(frozen=True, slots=True)
class StorageState:
    storage_id: str
    charge_kw: Decimal
    discharge_kw: Decimal
    soc_kwh: Decimal
    soc_end_kwh: Decimal
    available: bool
    state: str
    reason: str | None


def _series_index(points: Sequence[Mapping[str, Any]], value_key: str = "power_kw") -> dict[str, Decimal]:
    return {str(row["starts_at"]): Decimal(str(row[value_key])) for row in points}


def _storage_available(meta: Mapping[str, Any], slot: str) -> tuple[bool, str | None]:
    if not (_within(slot, str(meta["window_start"]), str(meta["window_end"]))):
        return False, "storage_unavailable"
    for window in meta.get("maintenance", ()):  # type: ignore[union-attr]
        if _within(slot, str(window["starts_at"]), str(window["ends_at"])):
            return False, "storage_maintenance"
    return True, None


def _line_limits(line: Mapping[str, Any], slot: str) -> tuple[Decimal, Decimal, bool]:
    """返回 (正向/外送上限, 反向/受入上限, 是否检修)。"""
    forward = Decimal(str(line["limits"][slot]))
    reverse = Decimal(str(line["reverse_limit_kw"]))
    for window in line.get("maintenance", ()):
        if _within(slot, str(window["starts_at"]), str(window["ends_at"])):
            return ZERO, ZERO, True
    return forward, reverse, False


def build_schedule(
    *,
    horizon_start: str,
    horizon_end: str,
    resources: Sequence[Mapping[str, Any]],
    customers: Sequence[Mapping[str, Any]],
    storages: Sequence[Mapping[str, Any]],
    line: Mapping[str, Any],
    opening_soc: Mapping[str, Decimal],
) -> dict[str, Any]:
    """根据输入快照构造分时方案。

    ``resources``/``customers`` 每项需含 ``points``（已展开到完整时间轴）；
    ``storages`` 项为可用性元数据；``line['limits']`` 为 slot->正向功率。
    """
    slots = slot_axis(horizon_start, horizon_end)
    resource_series = [(str(item["resource_id"]), _series_index(item["points"])) for item in resources]
    customer_series = [(str(item["customer_id"]), _series_index(item["points"], "guaranteed_kw"),
                       _series_index(item["points"], "max_demand_kw")) for item in customers]
    storage_meta = [(str(item["storage_id"]), item) for item in sorted(storages, key=lambda x: str(x["storage_id"]))]
    soc = {key: q(value) for key, value in opening_soc.items()}

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
    output_slots: list[dict[str, Any]] = []

    for slot in slots:
        reasons: list[str] = []
        generation = sum((series[slot] for _, series in resource_series), ZERO)

        # ---- 保供缺口 ---------------------------------------------------
        firm = sum((guaranteed[slot] for _, guaranteed, _ in customer_series), ZERO)
        deficit = max(ZERO, firm - generation)
        surplus = max(ZERO, generation - firm)
        served = min(firm, generation)

        storage_rows: dict[str, dict[str, Any]] = {}
        discharge_total = ZERO

        # 不足时：储能按编号顺序放电，再考虑受入电。
        line_forward, line_reverse, line_down = _line_limits(line, slot)
        if line_down:
            reasons.append("line_maintenance")
        for storage_id, meta in storage_meta:
            available, why = _storage_available(meta, slot)
            rated_discharge = Decimal(str(meta["rated_discharge_kw"]))
            eta = Decimal(str(meta["efficiency_percent"])) / Decimal("100")
            capacity = Decimal(str(meta["capacity_kwh"]))
            start_soc = soc.get(storage_id, Decimal(str(meta["initial_soc_kwh"])))
            if not available:
                reasons.append(why or "storage_unavailable")
                storage_rows[storage_id] = {
                    "charge_kw": "0.000",
                    "discharge_kw": "0.000",
                    "soc_kwh": decimal_text(start_soc),
                    "soc_end_kwh": decimal_text(start_soc),
                    "available": False,
                    "state": "idle",
                    "reason": why,
                }
                continue
            if deficit > ZERO:
                deliverable = min(rated_discharge, q(start_soc * eta / SLOT_HOURS), deficit)
            else:
                deliverable = ZERO
            end_soc = q(start_soc - deliverable * SLOT_HOURS / eta) if deliverable > ZERO else start_soc
            if deliverable > ZERO:
                reasons.append("storage_discharge")
                deficit -= deliverable
                discharge_total += deliverable
            storage_rows[storage_id] = {
                "charge_kw": "0.000",
                "discharge_kw": decimal_text(q(deliverable)),
                "soc_kwh": decimal_text(start_soc),
                "soc_end_kwh": decimal_text(end_soc),
                "available": True,
                "state": "discharging" if deliverable > ZERO else "idle",
                "reason": None,
            }
            soc[storage_id] = end_soc

        imported = min(deficit, ZERO if line_down else line_reverse)
        if imported > ZERO:
            reasons.append("line_import")
        unmet = q(deficit - imported)
        if unmet > ZERO:
            reasons.append("unmet_demand")
        served += discharge_total + imported

        # ---- 富余消纳 ---------------------------------------------------
        remaining = surplus
        charge_total = ZERO
        for storage_id, meta in storage_meta:
            row = storage_rows[storage_id]
            if not row["available"]:
                continue
            rated_charge = Decimal(str(meta["rated_charge_kw"]))
            eta = Decimal(str(meta["efficiency_percent"])) / Decimal("100")
            start_soc = Decimal(str(row["soc_end_kwh"]))
            # 可存储电能 (capacity-soc) 对应最大输入功率。
            by_capacity = q((Decimal(str(meta["capacity_kwh"])) - start_soc) / eta / SLOT_HOURS)
            charge_kw = min(rated_charge, by_capacity, remaining)
            if charge_kw > ZERO:
                end_soc = q(start_soc + charge_kw * SLOT_HOURS * eta)
                row["charge_kw"] = decimal_text(q(charge_kw))
                row["soc_end_kwh"] = decimal_text(end_soc)
                row["state"] = "charging"
                reasons.append("storage_charge")
                soc[storage_id] = end_soc
                charge_total += charge_kw
                remaining -= charge_kw

        # 工业可调负荷上浮（不超过各用户最大用电）。
        bands = {
            customer_id: (max_demand[slot] - guaranteed_value[slot])
            for customer_id, guaranteed_value, max_demand in customer_series
        }
        total_band = sum(bands.values(), ZERO)
        adjustable = min(remaining, total_band) if total_band > ZERO else ZERO
        if adjustable > ZERO:
            reasons.append("adjustable_load_raised")
            remaining -= adjustable

        # 汇总各用户实际供电：按保供比例分摊未满足需求，再叠加可调上浮。
        customer_rows: dict[str, dict[str, Any]] = {}
        ordered_customers = sorted(customer_series, key=lambda item: item[0])
        allocated_band = ZERO
        allocated_unmet = ZERO
        for index, (customer_id, guaranteed_value, max_demand) in enumerate(ordered_customers):
            firm_value = guaranteed_value[slot]
            if index == len(ordered_customers) - 1:
                extra = adjustable - allocated_band
                unmet_share = unmet - allocated_unmet
            elif firm > ZERO:
                extra = q(adjustable * bands[customer_id] / total_band) if total_band > ZERO else ZERO
                unmet_share = q(unmet * firm_value / firm)
                allocated_band += extra
                allocated_unmet += unmet_share
            else:
                extra = ZERO
                unmet_share = ZERO
            customer_rows[customer_id] = {
                "guaranteed_kw": decimal_text(firm_value),
                "max_demand_kw": decimal_text(max_demand[slot]),
                "served_kw": decimal_text(q(firm_value - unmet_share + extra)),
                "unmet_kw": decimal_text(q(unmet_share)),
                "adjustable_kw": decimal_text(q(extra)),
            }
        served += adjustable

        exported = min(remaining, ZERO if line_down else line_forward)
        if exported > ZERO:
            reasons.append("line_export")
        curtailed = q(remaining - exported)
        if curtailed > ZERO:
            reasons.append("renewable_curtailed")

        exchange = exported - imported
        forward_margin = q((ZERO if line_down else line_forward) - exported)
        reverse_margin = q((ZERO if line_down else line_reverse) - imported)

        for key, value in (
            ("generation_kwh", generation * SLOT_HOURS),
            ("served_kwh", served * SLOT_HOURS),
            ("unmet_kwh", unmet * SLOT_HOURS),
            ("curtailed_kwh", curtailed * SLOT_HOURS),
            ("charge_kwh", charge_total * SLOT_HOURS),
            ("discharge_kwh", discharge_total * SLOT_HOURS),
            ("export_kwh", exported * SLOT_HOURS),
            ("import_kwh", imported * SLOT_HOURS),
        ):
            totals[key] += value

        output_slots.append({
            "starts_at": slot,
            "generation_kw": decimal_text(q(generation)),
            "served_kw": decimal_text(q(served)),
            "guaranteed_kw": decimal_text(q(firm)),
            "adjustable_kw": decimal_text(q(adjustable)),
            "storage_charge_kw": decimal_text(q(charge_total)),
            "storage_discharge_kw": decimal_text(q(discharge_total)),
            "exchange_kw": decimal_text(q(exchange)),
            "export_kw": decimal_text(q(exported)),
            "import_kw": decimal_text(q(imported)),
            "renewable_curtailed_kw": decimal_text(curtailed),
            "unmet_kw": decimal_text(unmet),
            "line_limit_kw": decimal_text(ZERO if line_down else line_forward),
            "line_reverse_limit_kw": decimal_text(ZERO if line_down else line_reverse),
            "line_forward_margin_kw": decimal_text(forward_margin),
            "line_reverse_margin_kw": decimal_text(reverse_margin),
            "customers": customer_rows,
            "storages": storage_rows,
            "reasons": reasons,
        })

    summary = {key: decimal_text(q(value)) for key, value in totals.items()}
    firm_total = sum(
        (sum((guaranteed[slot] for _, guaranteed, _ in customer_series), ZERO) for slot in slots),
        ZERO,
    ) * SLOT_HOURS
    unmet_total = totals["unmet_kwh"]
    summary["guarantee_fulfillment_percent"] = format(
        (Decimal("100") if firm_total == ZERO else q((firm_total - unmet_total) / firm_total * 100)).quantize(Decimal("0.0001")),
        "f",
    )
    return {
        "horizon_start": horizon_start,
        "horizon_end": horizon_end,
        "slot_minutes": 15,
        "slots": output_slots,
        "summary": summary,
        "closing_soc": {key: decimal_text(value) for key, value in sorted(soc.items())},
    }
