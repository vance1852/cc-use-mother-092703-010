"""联合消纳领域输入契约：带版本的预测、储能窗口、线路能力与保供约束。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RESOURCE_KINDS = {"wind", "solar", "storage", "line", "load"}
STORAGE_KINDS = {"battery", "thermal"}
LOAD_KINDS = {"industrial", "critical"}


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


def power_series(raw: object, field: str) -> tuple[Decimal, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValidationFailed(f"{field} 必须是非空数值数组")
    points = tuple(
        decimal_value(item, f"{field}[{index}]", minimum=Decimal("0"))
        for index, item in enumerate(raw)
    )
    return points


def optional_series(raw: object, field: str, length: int) -> tuple[Decimal | None, ...]:
    if raw is None:
        return (None,) * length
    if not isinstance(raw, (list, tuple)) or len(raw) != length:
        raise ValidationFailed(f"{field} 必须是 {length} 个数值的数组")
    points: list[Decimal | None] = []
    for index, item in enumerate(raw):
        if item is None:
            points.append(None)
        else:
            points.append(decimal_value(item, f"{field}[{index}]", minimum=Decimal("0")))
    return tuple(points)


@dataclass(frozen=True, slots=True)
class GenerationForecast:
    """某类新能源场站某版本的分时功率预测（MW），槽位与方案网格对齐。"""

    forecast_id: str
    resource_id: str
    resource_kind: str
    version: int
    interval_minutes: int
    power_mw: tuple[Decimal, ...]
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GenerationForecast":
        kind = required_text(raw.get("resource_kind"), "resource_kind", 16).lower()
        if kind not in {"wind", "solar"}:
            raise ValidationFailed("resource_kind 必须是 wind 或 solar")
        version = positive_integer(raw.get("version", 1), "version")
        interval = positive_integer(raw.get("interval_minutes", 60), "interval_minutes")
        if 1440 % interval != 0:
            raise ValidationFailed("interval_minutes 必须整除 1440")
        power = power_series(raw.get("power_mw"), "power_mw")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        return cls(
            forecast_id=identifier(raw.get("forecast_id"), "forecast_id"),
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            resource_kind=kind,
            version=version,
            interval_minutes=interval,
            power_mw=power,
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class StorageWindow:
    """储能可用窗口：限定可充放时段与期初电量（MWh）；额定参数在储能台账上。"""

    storage_id: str
    version: int
    interval_minutes: int
    initial_soc_mwh: Decimal
    available: tuple[bool, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StorageWindow":
        interval = positive_integer(raw.get("interval_minutes", 60), "interval_minutes")
        flags = raw.get("available")
        if not isinstance(flags, (list, tuple)) or not flags:
            raise ValidationFailed("available 必须是非空布尔数组")
        available = tuple(bool(item) for item in flags)
        initial = decimal_value(
            raw.get("initial_soc_mwh", 0), "initial_soc_mwh", minimum=Decimal("0")
        )
        return cls(
            storage_id=identifier(raw.get("storage_id"), "storage_id"),
            version=positive_integer(raw.get("version", 1), "version"),
            interval_minutes=interval,
            initial_soc_mwh=initial,
            available=available,
            reason=required_text(raw.get("reason", ""), "reason", 256),
        )


@dataclass(frozen=True, slots=True)
class LineCapability:
    """输变电线路分时输送能力（MW）；None 表示沿用额定能力。"""

    line_id: str
    interval_minutes: int
    rated_mw: Decimal
    limits_mw: tuple[Decimal | None, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LineCapability":
        interval = positive_integer(raw.get("interval_minutes", 60), "interval_minutes")
        rated = decimal_value(raw.get("rated_mw"), "rated_mw", minimum=Decimal("0"))
        raw_limits = raw.get("limits_mw")
        if not isinstance(raw_limits, (list, tuple)) or not raw_limits:
            raise ValidationFailed("limits_mw 必须是非空数组（不限额的槽位用 null）")
        limits = tuple(
            None
            if item is None
            else decimal_value(item, "limits_mw", minimum=Decimal("0"), maximum=rated)
            for item in raw_limits
        )
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            interval_minutes=interval,
            rated_mw=rated,
            limits_mw=limits,
            reason=required_text(raw.get("reason", ""), "reason", 256),
        )


@dataclass(frozen=True, slots=True)
class LoadCommitment:
    """工业用户可调负荷与保供承诺。

    firm_mw 为必须满足的保供下限；curtailable_mw 为可在 firm 之上削减的负荷；
    reductions 显式登记保供承诺变化（负值为承诺上调，正值为放宽）。
    """

    load_id: str
    load_kind: str
    interval_minutes: int
    firm_mw: tuple[Decimal, ...]
    curtailable_mw: tuple[Decimal, ...]
    reductions: tuple[Decimal | None, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LoadCommitment":
        kind = required_text(raw.get("load_kind", "industrial"), "load_kind", 16).lower()
        if kind not in LOAD_KINDS:
            raise ValidationFailed("load_kind 必须是 industrial 或 critical")
        interval = positive_integer(raw.get("interval_minutes", 60), "interval_minutes")
        firm = power_series(raw.get("firm_mw"), "firm_mw")
        curtailable_raw = raw.get("curtailable_mw")
        if curtailable_raw is None:
            curtailable = tuple(Decimal("0") for _ in firm)
        else:
            curtailable = power_series(curtailable_raw, "curtailable_mw")
            if len(curtailable) != len(firm):
                raise ValidationFailed("curtailable_mw 与 firm_mw 槽位数必须一致")
        reductions = optional_series(raw.get("reductions"), "reductions", len(firm))
        for index, item in enumerate(reductions):
            if item is not None and item > curtailable[index]:
                raise ValidationFailed(f"reductions[{index}] 不能超过可削减负荷")
        return cls(
            load_id=identifier(raw.get("load_id"), "load_id"),
            load_kind=kind,
            interval_minutes=interval,
            firm_mw=firm,
            curtailable_mw=curtailable,
            reductions=reductions,
            reason=required_text(raw.get("reason", ""), "reason", 256),
        )
