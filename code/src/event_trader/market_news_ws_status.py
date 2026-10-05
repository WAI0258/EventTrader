"""Durable status snapshot for realtime market-news WebSocket acquisition."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts._validators import validate_timestamp
from event_trader.storage import WorkspaceLayout


class MarketNewsWebSocketStatusError(ValueError):
    """Raised when market-news WebSocket status state is invalid."""


_PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(slots=True)
class MarketNewsWebSocketStatusWriter:
    """Write a small overwrite-only operational snapshot for one WS provider."""

    layout: WorkspaceLayout
    provider: str
    tickers: tuple[str, ...]
    route_target_keys: tuple[str, ...]
    _state: dict[str, object] = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.layout, WorkspaceLayout):
            raise MarketNewsWebSocketStatusError("layout must be a WorkspaceLayout instance.")
        self.provider = _validate_provider(self.provider)
        self.tickers = _validate_text_tuple(self.tickers, field_name="tickers")
        self.route_target_keys = _validate_text_tuple(
            self.route_target_keys,
            field_name="route_target_keys",
        )
        self._lock = threading.Lock()
        self._state = {
            "provider": self.provider,
            "tickers": list(self.tickers),
            "route_target_keys": list(self.route_target_keys),
            "running": False,
            "session_active": False,
            "start_blocked": False,
            "start_attempt_count": 0,
            "connect_count": 0,
            "pong_count": 0,
            "message_count": 0,
            "parsed_event_count": 0,
            "matched_event_count": 0,
            "archived_count": 0,
            "released_count": 0,
            "error_count": 0,
            "last_start_attempt_at": None,
            "last_started_at": None,
            "last_connected_at": None,
            "last_pong_at": None,
            "last_message_at": None,
            "last_parsed_event_at": None,
            "last_matched_event_at": None,
            "last_archived_at": None,
            "last_released_at": None,
            "last_stopped_at": None,
            "last_error_at": None,
            "last_error": "",
            "last_stop_reason": "",
        }

    @property
    def path(self) -> Path:
        return (
            self.layout.runtime_root
            / "live_source"
            / "market_news_ws"
            / f"{self.provider}.json"
        ).resolve(strict=False)

    def record_session_state(
        self,
        *,
        current_time: datetime,
        session_active: bool,
        running: bool,
        start_blocked: bool,
    ) -> None:
        self._write(
            current_time,
            session_active=bool(session_active),
            running=bool(running),
            start_blocked=bool(start_blocked),
        )

    def record_start_attempt(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            last_start_attempt_at=current_time,
            running=False,
            start_blocked=False,
            _increments={"start_attempt_count": 1},
        )

    def record_start_blocked(self, *, current_time: datetime, reason: str) -> None:
        self._write(
            current_time,
            running=False,
            start_blocked=True,
            last_error_at=current_time,
            last_error=reason,
            _increments={"error_count": 1},
        )

    def record_started(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            start_blocked=False,
            last_started_at=current_time,
            last_stop_reason="",
        )

    def record_connected(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            start_blocked=False,
            last_connected_at=current_time,
            last_error="",
            _increments={"connect_count": 1},
        )

    def record_pong(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            last_pong_at=current_time,
            _increments={"pong_count": 1},
        )

    def record_message(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            last_message_at=current_time,
            _increments={"message_count": 1},
        )

    def record_parsed_events(self, *, current_time: datetime, event_count: int) -> None:
        if event_count <= 0:
            return
        self._write(
            current_time,
            running=True,
            last_parsed_event_at=current_time,
            _increments={"parsed_event_count": event_count},
        )

    def record_matched_event(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            last_matched_event_at=current_time,
            _increments={"matched_event_count": 1},
        )

    def record_archived(self, *, current_time: datetime) -> None:
        self._write(
            current_time,
            running=True,
            last_archived_at=current_time,
            _increments={"archived_count": 1},
        )

    def record_released(self, *, current_time: datetime, released_count: int) -> None:
        self._write(
            current_time,
            running=True,
            last_released_at=current_time,
            _increments={"released_count": max(0, released_count)},
        )

    def record_error(self, *, current_time: datetime, error: str) -> None:
        self._write(
            current_time,
            last_error_at=current_time,
            last_error=error.strip(),
            _increments={"error_count": 1},
        )

    def record_stopped(self, *, current_time: datetime, reason: str) -> None:
        self._write(
            current_time,
            running=False,
            last_stopped_at=current_time,
            last_stop_reason=reason.strip(),
        )

    def _write(
        self,
        current_time: datetime,
        /,
        *,
        _increments: dict[str, int] | None = None,
        **changes: object,
    ) -> None:
        recorded_at = _validate_utc(current_time, field_name="current_time")
        serialized_changes = {
            key: _serialize_value(value) for key, value in changes.items()
        }
        with self._lock:
            self._state.update(serialized_changes)
            for key, amount in (_increments or {}).items():
                value = self._state.get(key, 0)
                if not isinstance(value, int):
                    raise MarketNewsWebSocketStatusError(
                        f"status counter {key!r} is not an integer."
                    )
                self._state[key] = value + amount
            self._state["recorded_at"] = recorded_at.isoformat()
            payload = dict(self._state)
            _write_json_atomic(self.path, payload)


def _validate_provider(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketNewsWebSocketStatusError("provider must be a non-empty string.")
    normalized = value.strip()
    if _PROVIDER_PATTERN.fullmatch(normalized) is None:
        raise MarketNewsWebSocketStatusError(
            "provider must contain only letters, numbers, dot, underscore, or dash."
        )
    return normalized


def _validate_text_tuple(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise MarketNewsWebSocketStatusError(f"{field_name} must be a non-empty tuple.")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise MarketNewsWebSocketStatusError(
                f"{field_name} must contain only non-empty strings."
            )
        text = value.strip()
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _validate_utc(value: datetime, *, field_name: str) -> datetime:
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=MarketNewsWebSocketStatusError,
    ).astimezone(UTC)


def _serialize_value(value: object) -> object:
    if isinstance(value, datetime):
        return _validate_utc(value, field_name="status timestamp").isoformat()
    return value


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise MarketNewsWebSocketStatusError(
            f"Failed to write market-news websocket status {path}: {exc}"
        ) from exc


__all__ = [
    "MarketNewsWebSocketStatusError",
    "MarketNewsWebSocketStatusWriter",
]
