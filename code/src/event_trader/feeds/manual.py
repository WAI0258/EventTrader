"""Operator-supplied manual material adapters at the feeds boundary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from .models import (
    FeedModelError,
    HistoricalManualDatasetInput,
    LiveManualSourceInput,
)
from .payload_mappers import (
    FeedAdapterError,
)
from .payload_mappers import (
    adapt_historical_manual_dataset as _adapt_replay_manual_dataset,
)

type ManualIngressInput = LiveManualSourceInput | HistoricalManualDatasetInput

_MANUAL_SOURCE_FIELDS = (
    "source_ref",
    "title",
    "content",
    "provided_at",
)


def adapt_manual_source(payload: Mapping[str, object]) -> LiveManualSourceInput:
    """Adapt one canonical operator-supplied payload into live feed ingress."""
    data = _payload_fields(
        payload,
        source_shape="manual_source",
        required_fields=_MANUAL_SOURCE_FIELDS,
    )
    return LiveManualSourceInput(
        source_ref=cast(str, data["source_ref"]),
        title=cast(str, data["title"]),
        content=cast(str, data["content"]),
        provided_at=cast(datetime, data["provided_at"]),
    )



def adapt_historical_manual_dataset(
    payload: Mapping[str, object],
) -> HistoricalManualDatasetInput:
    """Adapt one canonical historical manual payload into replay feed ingress."""
    return _adapt_replay_manual_dataset(payload)


def _payload_fields(
    payload: Mapping[str, object],
    *,
    source_shape: str,
    required_fields: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise FeedAdapterError(
            f"{source_shape} adapter payload must be a mapping of canonical fields."
        )

    missing = tuple(field for field in required_fields if field not in payload)
    unexpected = tuple(str(field) for field in payload if field not in required_fields)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing required field(s): {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected field(s): {', '.join(unexpected)}")
        raise FeedAdapterError(
            f"{source_shape} adapter payload does not match the committed boundary; "
            f"{'; '.join(problems)}."
        )

    return {field: payload[field] for field in required_fields}


__all__ = [
    "FeedAdapterError",
    "FeedModelError",
    "HistoricalManualDatasetInput",
    "LiveManualSourceInput",
    "ManualIngressInput",
    "adapt_historical_manual_dataset",
    "adapt_manual_source",
]
