"""Strict price-semantics contract for exposure-blind analysis assessments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.analysis_assessment_schema import (
    analysis_price_semantics_required_fields,
)
from event_trader.contracts.instrument_basis import canonicalize_instrument_basis


class AnalysisPriceSemanticsContractError(ValueError):
    """Raised when analysis price semantics are malformed."""


@dataclass(frozen=True, slots=True)
class AnalysisPriceSemantics:
    target_key: str
    instrument_basis: str
    analysis_reference_price: float
    analysis_reference_at: datetime
    current_leg_start_price: float | None
    current_leg_end_price: float | None
    swing_high: float | None
    swing_low: float | None
    window_high: float | None
    window_low: float | None
    active_price_level_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=AnalysisPriceSemanticsContractError,
            ),
        )
        object.__setattr__(
            self,
            "instrument_basis",
            canonicalize_instrument_basis(
                _validate_non_blank(self.instrument_basis, "instrument_basis")
            ),
        )
        object.__setattr__(
            self,
            "analysis_reference_price",
            _validate_float(self.analysis_reference_price, "analysis_reference_price"),
        )
        object.__setattr__(
            self,
            "analysis_reference_at",
            validate_timestamp(
                self.analysis_reference_at,
                field_name="analysis_reference_at",
                error_type=AnalysisPriceSemanticsContractError,
            ),
        )
        for field_name in (
            "current_leg_start_price",
            "current_leg_end_price",
            "swing_high",
            "swing_low",
            "window_high",
            "window_low",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_float(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "active_price_level_ids",
            _validate_string_tuple(
                self.active_price_level_ids,
                "active_price_level_ids",
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "instrument_basis": self.instrument_basis,
            "analysis_reference_price": self.analysis_reference_price,
            "analysis_reference_at": self.analysis_reference_at.isoformat(),
            "current_leg_start_price": self.current_leg_start_price,
            "current_leg_end_price": self.current_leg_end_price,
            "swing_high": self.swing_high,
            "swing_low": self.swing_low,
            "window_high": self.window_high,
            "window_low": self.window_low,
            "active_price_level_ids": list(self.active_price_level_ids),
        }


def parse_analysis_price_semantics(payload: Mapping[str, object]) -> AnalysisPriceSemantics:
    _require_exact_fields(
        payload,
        required=set(analysis_price_semantics_required_fields()),
        surface="analysis_price_semantics",
    )
    return AnalysisPriceSemantics(
        target_key=_require_text(payload, "target_key"),
        instrument_basis=_require_text(payload, "instrument_basis"),
        analysis_reference_price=_require_float(payload, "analysis_reference_price"),
        analysis_reference_at=_parse_timestamp(
            payload.get("analysis_reference_at"),
            "analysis_reference_at",
        ),
        current_leg_start_price=_optional_float(
            payload.get("current_leg_start_price"),
            "current_leg_start_price",
        ),
        current_leg_end_price=_optional_float(
            payload.get("current_leg_end_price"),
            "current_leg_end_price",
        ),
        swing_high=_optional_float(payload.get("swing_high"), "swing_high"),
        swing_low=_optional_float(payload.get("swing_low"), "swing_low"),
        window_high=_optional_float(payload.get("window_high"), "window_high"),
        window_low=_optional_float(payload.get("window_low"), "window_low"),
        active_price_level_ids=tuple(
            _require_string_list(payload, "active_price_level_ids")
        ),
    )


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisPriceSemanticsContractError(
            f"{field_name} must be a non-blank string."
        )
    normalized = value.strip()
    if normalized != value:
        raise AnalysisPriceSemanticsContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _validate_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AnalysisPriceSemanticsContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise AnalysisPriceSemanticsContractError(f"{field_name} must be finite.")
    return normalized


def _validate_optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _validate_float(value, field_name)


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        text = _validate_non_blank(value, field_name)
        if text in seen:
            raise AnalysisPriceSemanticsContractError(
                f"{field_name} must not contain duplicates."
            )
        seen.add(text)
    return normalized


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    return _validate_float(payload.get(field_name), field_name)


def _optional_float(value: object, field_name: str) -> float | None:
    return _validate_optional_float(value, field_name)


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AnalysisPriceSemanticsContractError(
            f"{field_name} must be a list of strings."
        )
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise AnalysisPriceSemanticsContractError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisPriceSemanticsContractError(
            f"{field_name} must be an ISO timestamp."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=AnalysisPriceSemanticsContractError,
    )


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    surface: str,
) -> None:
    actual = set(str(key) for key in payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise AnalysisPriceSemanticsContractError(f"{surface} " + "; ".join(problems))


__all__ = [
    "AnalysisPriceSemantics",
    "AnalysisPriceSemanticsContractError",
    "parse_analysis_price_semantics",
]
