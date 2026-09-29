"""联合消纳领域输入契约与校验。

所有功率单位统一为千瓦(kW)，电量单位为千瓦时(kWh)，内部统一使用
``Decimal`` 文本落库，避免浮点累计误差。时间轴按 15 分钟对齐。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SLOT_MINUTES = 15
RESOURCE_KINDS = {"solar", "wind"}
STORAGE_STATES = {"idle", "charging", "discharging"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
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
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def utc_value(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parsed = parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return parsed


def require_slot_boundary(value: Any, field: str) -> Any:
    """``parse_utc`` 的包装，同时要求分钟落在 15 分钟整点上。"""
    parsed = utc_value(value, field)
    if parsed.minute % SLOT_MINUTES != 0 or parsed.second or parsed.microsecond:
        raise ValidationFailed(f"{field} 必须对齐到 {SLOT_MINUTES} 分钟整点")
    return parsed


def validate_points(points: object, field: str, *, nonnegative: bool = True) -> list[dict[str, Any]]:
    if not isinstance(points, list) or not points:
        raise ValidationFailed(f"{field} 必须是非空数组")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(points):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field}[{index}] 必须是对象")
        starts_at = require_slot_boundary(item.get("starts_at"), f"{field}[{index}].starts_at")
        key = starts_at.isoformat().replace("+00:00", "Z")
        if key in seen:
            raise ValidationFailed(f"{field}[{index}] 时间点重复")
        seen.add(key)
        power_kw = decimal_value(
            item.get("power_kw"),
            f"{field}[{index}].power_kw",
            minimum=Decimal("0") if nonnegative else None,
        )
        rows.append({"starts_at": key, "_start": starts_at, "power_kw": power_kw})
    rows.sort(key=lambda row: row["starts_at"])
    for previous, current in zip(rows, rows[1:]):
        gap = current["_start"] - previous["_start"]
        if gap.total_seconds() != SLOT_MINUTES * 60:
            raise ValidationFailed(f"{field} 必须是连续的 {SLOT_MINUTES} 分钟序列")
    for row in rows:
        row.pop("_start")
    return rows


@dataclass(frozen=True, slots=True)
class GenerationForecast:
    """带版本的新能源发电预测（按场站资源的一条功率序列）。"""

    resource_id: str
    kind: str
    version: str
    points: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GenerationForecast":
        kind = required_text(raw.get("kind"), "kind", 16).lower()
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("kind 必须是 solar 或 wind")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            kind=kind,
            version=identifier(raw.get("version"), "version"),
            points=tuple(validate_points(raw.get("points"), "points")),
        )


@dataclass(frozen=True, slots=True)
class StorageAvailability:
    """储能资源的可用窗口。

    额定功率、容量与效率来自资源台账；本契约只冻结可用时间范围、
    初始 SOC 与检修窗口。
    """

    storage_id: str
    version: str
    initial_soc_kwh: Decimal
    window_start: Any
    window_end: Any
    maintenance: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StorageAvailability":
        initial = decimal_value(
            raw.get("initial_soc_kwh", 0), "initial_soc_kwh", minimum=Decimal("0")
        )
        window_start = require_slot_boundary(raw.get("window_start"), "window_start")
        window_end = require_slot_boundary(raw.get("window_end"), "window_end")
        if window_end <= window_start:
            raise ValidationFailed("window_end 必须晚于 window_start")
        maintenance_raw = raw.get("maintenance", [])
        if not isinstance(maintenance_raw, list):
            raise ValidationFailed("maintenance 必须是数组")
        maintenance: list[dict[str, Any]] = []
        for index, item in enumerate(maintenance_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"maintenance[{index}] 必须是对象")
            starts = require_slot_boundary(item.get("starts_at"), f"maintenance[{index}].starts_at")
            ends = require_slot_boundary(item.get("ends_at"), f"maintenance[{index}].ends_at")
            if ends <= starts:
                raise ValidationFailed(f"maintenance[{index}] 结束时间必须晚于开始时间")
            maintenance.append({
                "starts_at": starts.isoformat().replace("+00:00", "Z"),
                "ends_at": ends.isoformat().replace("+00:00", "Z"),
                "reason": required_text(item.get("reason", "设备检修"), f"maintenance[{index}].reason"),
            })
        return cls(
            storage_id=identifier(raw.get("storage_id"), "storage_id"),
            version=identifier(raw.get("version"), "version"),
            initial_soc_kwh=initial,
            window_start=window_start,
            window_end=window_end,
            maintenance=tuple(maintenance),
        )


@dataclass(frozen=True, slots=True)
class LineCapability:
    """输变电线路（线路或主变）的分时段输送能力。

    ``points`` 给出正向（外送）能力上限；``reverse_limit_kw`` 给出受入
    （反向）能力上限，默认与正向相同。``maintenance`` 记录设备检修。
    """

    line_id: str
    version: str
    points: tuple[dict[str, Any], ...]
    reverse_limit_kw: Decimal
    maintenance: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LineCapability":
        points = tuple(validate_points(raw.get("points"), "points"))
        reverse = raw.get("reverse_limit_kw")
        reverse_limit = (
            decimal_value(reverse, "reverse_limit_kw", minimum=Decimal("0"))
            if reverse is not None
            else max((row["power_kw"] for row in points), default=Decimal("0"))
        )
        maintenance_raw = raw.get("maintenance", [])
        if not isinstance(maintenance_raw, list):
            raise ValidationFailed("maintenance 必须是数组")
        maintenance: list[dict[str, Any]] = []
        for index, item in enumerate(maintenance_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"maintenance[{index}] 必须是对象")
            starts = require_slot_boundary(item.get("starts_at"), f"maintenance[{index}].starts_at")
            ends = require_slot_boundary(item.get("ends_at"), f"maintenance[{index}].ends_at")
            if ends <= starts:
                raise ValidationFailed(f"maintenance[{index}] 结束时间必须晚于开始时间")
            maintenance.append({
                "starts_at": starts.isoformat().replace("+00:00", "Z"),
                "ends_at": ends.isoformat().replace("+00:00", "Z"),
                "reason": required_text(item.get("reason", "设备检修"), f"maintenance[{index}].reason"),
            })
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            version=identifier(raw.get("version"), "version"),
            points=points,
            reverse_limit_kw=reverse_limit,
            maintenance=tuple(maintenance),
        )


@dataclass(frozen=True, slots=True)
class CustomerCommitment:
    """工业用户可调负荷与保供承诺。

    - ``guaranteed_kw``：保供下限，任何方案都必须满足（无法满足时记为未满足需求）；
    - ``max_demand_kw``：该时段允许安排的最大用电；
    - ``curtailable_kw``：可中断/下调的可调负荷上限（相对基准负荷）；
    - 若给出 ``points``，则每个时段单独约定，否则按全天恒定值展开。
    """

    customer_id: str
    version: str
    points: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], horizon: tuple[str, str] | None = None) -> "CustomerCommitment":
        customer_id = identifier(raw.get("customer_id"), "customer_id")
        version = identifier(raw.get("version"), "version")
        raw_points = raw.get("points")
        if isinstance(raw_points, list) and raw_points:
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index, item in enumerate(raw_points):
                if not isinstance(item, Mapping):
                    raise ValidationFailed(f"points[{index}] 必须是对象")
                starts_at = require_slot_boundary(item.get("starts_at"), f"points[{index}].starts_at")
                key = starts_at.isoformat().replace("+00:00", "Z")
                if key in seen:
                    raise ValidationFailed(f"points[{index}] 时间点重复")
                seen.add(key)
                guaranteed = decimal_value(
                    item.get("guaranteed_kw", item.get("power_kw")),
                    f"points[{index}].guaranteed_kw",
                    minimum=Decimal("0"),
                )
                max_demand = decimal_value(
                    item.get("max_demand_kw", guaranteed),
                    f"points[{index}].max_demand_kw",
                    minimum=guaranteed,
                )
                curtailable = decimal_value(
                    item.get("curtailable_kw", 0), f"points[{index}].curtailable_kw", minimum=Decimal("0")
                )
                rows.append({
                    "starts_at": key,
                    "_start": starts_at,
                    "guaranteed_kw": guaranteed,
                    "max_demand_kw": max_demand,
                    "curtailable_kw": curtailable,
                })
            rows.sort(key=lambda row: row["starts_at"])
            for previous, current in zip(rows, rows[1:]):
                gap = current["_start"] - previous["_start"]
                if gap.total_seconds() != SLOT_MINUTES * 60:
                    raise ValidationFailed(f"points 必须是连续的 {SLOT_MINUTES} 分钟序列")
            for row in rows:
                row.pop("_start")
            return cls(customer_id=customer_id, version=version, points=tuple(rows))
        guaranteed = decimal_value(raw.get("guaranteed_kw"), "guaranteed_kw", minimum=Decimal("0"))
        max_demand = decimal_value(
            raw.get("max_demand_kw", guaranteed), "max_demand_kw", minimum=guaranteed
        )
        curtailable = decimal_value(raw.get("curtailable_kw", 0), "curtailable_kw", minimum=Decimal("0"))
        if horizon is None:
            raise ValidationFailed("恒定承诺必须在生成方案时提供时间轴")
        from datetime import datetime, timedelta

        start = datetime.fromisoformat(horizon[0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(horizon[1].replace("Z", "+00:00"))
        rows = []
        cursor = start
        while cursor < end:
            rows.append({
                "starts_at": cursor.isoformat().replace("+00:00", "Z"),
                "guaranteed_kw": guaranteed,
                "max_demand_kw": max_demand,
                "curtailable_kw": curtailable,
            })
            cursor += timedelta(minutes=SLOT_MINUTES)
        return cls(customer_id=customer_id, version=version, points=tuple(rows))
