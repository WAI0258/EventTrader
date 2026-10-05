"""Canonical live and replay ingress input models owned by the feeds boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from event_trader.contracts._validators import (
    normalize_content,
    validate_source_ref,
    validate_timestamp,
)
class FeedModelError(ValueError):
    """Raised when a feed-owned ingress input is structurally invalid."""


type LiveSourceShape = Literal[
    "news_stream",
    "web_search",
    "macro_api",
    "manual_source",
]

type ReplaySourceShape = Literal[
    "historical_news_stream",
    "historical_market_news",
    "historical_macro_api",
    "historical_manual_dataset",
    "historical_web_search",
]


@dataclass(frozen=True, slots=True)
class LiveNewsStreamInput:
    """Live ingress shape for continuous headline or development streams."""

    source_shape: Literal["news_stream"] = field(init=False, default="news_stream")
    source_ref: str
    headline: str
    body: str
    published_at: datetime
    captured_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "headline",
            _validate_non_blank_text(self.headline, field_name="headline"),
        )
        object.__setattr__(
            self,
            "body",
            normalize_content(
                self.body,
                field_name="body",
                error_type=FeedModelError,
            ),
        )
        validate_timestamp(
            self.published_at,
            field_name="published_at",
            error_type=FeedModelError,
        )
        validate_timestamp(
            self.captured_at,
            field_name="captured_at",
            error_type=FeedModelError,
        )


@dataclass(frozen=True, slots=True)
class LiveWebSearchInput:
    """Live ingress shape for search-driven discovery that resolved concrete source material."""

    source_shape: Literal["web_search"] = field(init=False, default="web_search")
    query: str
    source_ref: str
    title: str
    content: str
    discovered_at: datetime
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "query",
            _validate_non_blank_text(self.query, field_name="query"),
        )
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=FeedModelError,
            ),
        )
        validate_timestamp(
            self.discovered_at,
            field_name="discovered_at",
            error_type=FeedModelError,
        )
        if self.published_at is not None:
            validate_timestamp(
                self.published_at,
                field_name="published_at",
                error_type=FeedModelError,
            )


@dataclass(frozen=True, slots=True)
class LiveMacroApiInput:
    """Live ingress shape for scheduled structured macro or event API releases."""

    source_shape: Literal["macro_api"] = field(init=False, default="macro_api")
    source_ref: str
    release_key: str
    period_label: str
    value_text: str
    unit: str | None
    released_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "release_key",
            _validate_non_blank_text(self.release_key, field_name="release_key"),
        )
        object.__setattr__(
            self,
            "period_label",
            _validate_non_blank_text(self.period_label, field_name="period_label"),
        )
        object.__setattr__(
            self,
            "value_text",
            _validate_non_blank_text(self.value_text, field_name="value_text"),
        )
        if self.unit is not None:
            object.__setattr__(
                self,
                "unit",
                _validate_non_blank_text(self.unit, field_name="unit"),
            )
        validate_timestamp(
            self.released_at,
            field_name="released_at",
            error_type=FeedModelError,
        )


@dataclass(frozen=True, slots=True)
class LiveManualSourceInput:
    """Live ingress shape for authoritative operator-supplied source material."""

    source_shape: Literal["manual_source"] = field(init=False, default="manual_source")
    source_ref: str
    title: str
    content: str
    provided_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=FeedModelError,
            ),
        )
        validate_timestamp(
            self.provided_at,
            field_name="provided_at",
            error_type=FeedModelError,
        )


@dataclass(frozen=True, slots=True)
class HistoricalNewsStreamInput:
    """Replay ingress shape for archived headline or development streams."""

    source_shape: Literal["historical_news_stream"] = field(
        init=False,
        default="historical_news_stream",
    )
    source_ref: str
    headline: str
    body: str
    published_at: datetime
    captured_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "headline",
            _validate_non_blank_text(self.headline, field_name="headline"),
        )
        object.__setattr__(
            self,
            "body",
            normalize_content(
                self.body,
                field_name="body",
                error_type=FeedModelError,
            ),
        )
        published_at = validate_timestamp(
            self.published_at,
            field_name="published_at",
            error_type=FeedModelError,
        )
        validate_timestamp(
            self.captured_at,
            field_name="captured_at",
            error_type=FeedModelError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=FeedModelError,
        )
        _validate_visibility_not_before_source(
            visible_at,
            source_timestamp=published_at,
            source_field_name="published_at",
        )


@dataclass(frozen=True, slots=True)
class HistoricalMarketNewsInput:
    """Replay ingress shape for archived structured market-news API articles."""

    source_shape: Literal["historical_market_news"] = field(
        init=False,
        default="historical_market_news",
    )
    source_ref: str
    headline: str
    body: str
    published_at: datetime
    updated_at: datetime | None
    captured_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "headline",
            _validate_non_blank_text(self.headline, field_name="headline"),
        )
        object.__setattr__(
            self,
            "body",
            normalize_content(
                self.body,
                field_name="body",
                error_type=FeedModelError,
            ),
        )
        published_at = validate_timestamp(
            self.published_at,
            field_name="published_at",
            error_type=FeedModelError,
        )
        source_timestamp = published_at
        if self.updated_at is not None:
            updated_at = validate_timestamp(
                self.updated_at,
                field_name="updated_at",
                error_type=FeedModelError,
            )
            source_timestamp = max(source_timestamp, updated_at)
        validate_timestamp(
            self.captured_at,
            field_name="captured_at",
            error_type=FeedModelError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=FeedModelError,
        )
        _validate_visibility_not_before_source(
            visible_at,
            source_timestamp=source_timestamp,
            source_field_name=(
                "updated_at" if self.updated_at is not None else "published_at"
            ),
        )


@dataclass(frozen=True, slots=True)
class HistoricalMacroApiInput:
    """Replay ingress shape for archived structured macro or event releases."""

    source_shape: Literal["historical_macro_api"] = field(
        init=False,
        default="historical_macro_api",
    )
    source_ref: str
    release_key: str
    period_label: str
    value_text: str
    unit: str | None
    released_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "release_key",
            _validate_non_blank_text(self.release_key, field_name="release_key"),
        )
        object.__setattr__(
            self,
            "period_label",
            _validate_non_blank_text(self.period_label, field_name="period_label"),
        )
        object.__setattr__(
            self,
            "value_text",
            _validate_non_blank_text(self.value_text, field_name="value_text"),
        )
        if self.unit is not None:
            object.__setattr__(
                self,
                "unit",
                _validate_non_blank_text(self.unit, field_name="unit"),
            )
        released_at = validate_timestamp(
            self.released_at,
            field_name="released_at",
            error_type=FeedModelError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=FeedModelError,
        )
        _validate_visibility_not_before_source(
            visible_at,
            source_timestamp=released_at,
            source_field_name="released_at",
        )


@dataclass(frozen=True, slots=True)
class HistoricalManualDatasetInput:
    """Replay ingress shape for manually curated historical source samples."""

    source_shape: Literal["historical_manual_dataset"] = field(
        init=False,
        default="historical_manual_dataset",
    )
    source_ref: str
    title: str
    content: str
    provided_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=FeedModelError,
            ),
        )
        provided_at = validate_timestamp(
            self.provided_at,
            field_name="provided_at",
            error_type=FeedModelError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=FeedModelError,
        )
        _validate_visibility_not_before_source(
            visible_at,
            source_timestamp=provided_at,
            source_field_name="provided_at",
        )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchInput:
    """Replay ingress shape for archived search-resolved source material."""

    source_shape: Literal["historical_web_search"] = field(
        init=False,
        default="historical_web_search",
    )
    query: str
    source_ref: str
    title: str
    content: str
    published_at: datetime | None
    discovered_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "query",
            _validate_non_blank_text(self.query, field_name="query"),
        )
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=FeedModelError),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=FeedModelError,
            ),
        )
        if self.published_at is not None:
            validate_timestamp(
                self.published_at,
                field_name="published_at",
                error_type=FeedModelError,
            )
        discovered_at = validate_timestamp(
            self.discovered_at,
            field_name="discovered_at",
            error_type=FeedModelError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=FeedModelError,
        )
        _validate_visibility_not_before_source(
            visible_at,
            source_timestamp=(
                discovered_at if self.published_at is None else self.published_at
            ),
            source_field_name=(
                "discovered_at" if self.published_at is None else "published_at"
            ),
        )


type LiveIngressInput = (
    LiveNewsStreamInput
    | LiveWebSearchInput
    | LiveMacroApiInput
    | LiveManualSourceInput
)

type ReplayIngressInput = (
    HistoricalNewsStreamInput
    | HistoricalMarketNewsInput
    | HistoricalMacroApiInput
    | HistoricalManualDatasetInput
    | HistoricalWebSearchInput
)

REPLAY_INGRESS_INPUT_TYPES = (
    HistoricalNewsStreamInput,
    HistoricalMarketNewsInput,
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalWebSearchInput,
)


def is_replay_ingress_input(value: object) -> bool:
    """Return whether a value is one canonical replay ingress model."""
    return isinstance(value, REPLAY_INGRESS_INPUT_TYPES)


def validate_replay_ingress_input(
    value: object,
    *,
    error_type: type[Exception] = FeedModelError,
    message: str | None = None,
) -> ReplayIngressInput:
    """Validate one runtime replay ingress value against the canonical model set."""
    if not is_replay_ingress_input(value):
        raise error_type(
            message
            or "value must be one of the canonical replay feed boundary models."
        )
    return value


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise FeedModelError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise FeedModelError(f"{field_name} must not be blank.")
    return normalized



def _validate_visibility_not_before_source(
    visible_at: datetime,
    *,
    source_timestamp: datetime,
    source_field_name: str,
) -> None:
    if visible_at < source_timestamp:
        raise FeedModelError(
            "visible_at must be greater than or equal to "
            f"{source_field_name} for replay ingress inputs."
        )


__all__ = [
    "FeedModelError",
    "HistoricalMacroApiInput",
    "HistoricalMarketNewsInput",
    "HistoricalManualDatasetInput",
    "HistoricalNewsStreamInput",
    "HistoricalWebSearchInput",
    "LiveIngressInput",
    "LiveMacroApiInput",
    "LiveManualSourceInput",
    "LiveNewsStreamInput",
    "LiveSourceShape",
    "LiveWebSearchInput",
    "REPLAY_INGRESS_INPUT_TYPES",
    "ReplayIngressInput",
    "ReplaySourceShape",
    "is_replay_ingress_input",
    "validate_replay_ingress_input",
]
