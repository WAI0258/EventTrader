"""Shared reader for persisted checker decision receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts import CheckerDecision
from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.decision_memory import FileBackedDecisionEpisodeStore
from event_trader.storage import WorkspaceLayout

_CHECKER_DECISION_DIR_NAME = "checker_decisions"


class CheckerReceiptStoreError(ValueError):
    """Raised when persisted checker decision receipts are invalid."""


@dataclass(frozen=True, slots=True)
class CheckerDecisionReceipt:
    target_key: str
    event_id: str
    source_ref: str
    title: str
    business_at: datetime
    decision: str
    rationale: str
    attention_hint: str | None
    requires_watchlist_maintenance: bool
    caused_analysis_request: bool
    validator_action: str
    decision_episode_id: str
    path: Path
    line_number: int

    def to_checker_decision(self) -> CheckerDecision:
        return CheckerDecision(
            target_key=self.target_key,
            event_ids=[self.event_id],
            decision=self.decision,
            rationale=self.rationale,
            attention_hint=self.attention_hint,
            requires_watchlist_maintenance=self.requires_watchlist_maintenance,
        )


def iter_checker_decision_receipts(
    *,
    layout: WorkspaceLayout,
    target_key: str | None = None,
    event_id: str | None = None,
    decision: str | None = None,
    caused_analysis_request: bool | None = None,
) -> tuple[CheckerDecisionReceipt, ...]:
    if not isinstance(layout, WorkspaceLayout):
        raise CheckerReceiptStoreError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = (
        None
        if target_key is None
        else validate_target_key(target_key, error_type=CheckerReceiptStoreError)
    )
    normalized_event_id = (
        None
        if event_id is None
        else validate_event_id(event_id, error_type=CheckerReceiptStoreError)
    )
    root = layout.runtime_root / _CHECKER_DECISION_DIR_NAME
    if not root.exists():
        return ()
    if not root.is_dir():
        raise CheckerReceiptStoreError(
            f"checker_decisions path must be a directory: {root}"
        )
    target_roots = (
        (root / normalized_target_key,)
        if normalized_target_key is not None
        else tuple(path for path in sorted(root.iterdir()) if path.is_dir())
    )
    receipts: list[CheckerDecisionReceipt] = []
    for target_root in target_roots:
        if not target_root.exists():
            continue
        if not target_root.is_dir():
            raise CheckerReceiptStoreError(
                f"checker decision target path must be a directory: {target_root}"
            )
        for shard_path in sorted(target_root.glob("*.jsonl")):
            receipts.extend(
                _load_checker_decision_receipts(
                    shard_path=shard_path,
                    event_id=normalized_event_id,
                    decision=decision,
                    caused_analysis_request=caused_analysis_request,
                )
            )
    return tuple(receipts)


def find_latest_checker_decision_receipt(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    decision: str | None = None,
    caused_analysis_request: bool | None = None,
) -> CheckerDecisionReceipt | None:
    receipts = iter_checker_decision_receipts(
        layout=layout,
        target_key=target_key,
        event_id=event_id,
        decision=decision,
        caused_analysis_request=caused_analysis_request,
    )
    if not receipts:
        return None
    return receipts[-1]


def find_latest_escalation_reuse_receipt(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
) -> CheckerDecisionReceipt | None:
    receipt = find_latest_checker_decision_receipt(
        layout=layout,
        target_key=target_key,
        event_id=event_id,
        decision="escalate",
    )
    if receipt is not None:
        return receipt
    return _find_latest_attention_receipt(
        layout=layout,
        target_key=target_key,
        event_id=event_id,
    )


def _load_checker_decision_receipts(
    *,
    shard_path: Path,
    event_id: str | None,
    decision: str | None,
    caused_analysis_request: bool | None,
) -> list[CheckerDecisionReceipt]:
    receipts: list[CheckerDecisionReceipt] = []
    with shard_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            payload = _load_json_object(
                normalized,
                path=shard_path,
                line_number=line_number,
            )
            payload_decision = _require_text(payload, "decision", path=shard_path)
            if decision is not None and payload_decision != decision:
                continue
            payload_event_id = _require_event_id(payload, "event_id", path=shard_path)
            if event_id is not None and payload_event_id != event_id:
                continue
            payload_caused_analysis_request = _require_bool(
                payload,
                "caused_analysis_request",
                path=shard_path,
            )
            if (
                caused_analysis_request is not None
                and payload_caused_analysis_request != caused_analysis_request
            ):
                continue
            receipts.append(
                CheckerDecisionReceipt(
                    target_key=_require_target_key(payload, "target_key", path=shard_path),
                    event_id=payload_event_id,
                    source_ref=_require_text(payload, "source_ref", path=shard_path),
                    title=_require_text(payload, "title", path=shard_path),
                    business_at=_require_datetime(payload, "business_at", path=shard_path),
                    decision=payload_decision,
                    rationale=_require_text(payload, "rationale", path=shard_path),
                    attention_hint=_optional_text(payload, "attention_hint"),
                    requires_watchlist_maintenance=_require_bool(
                        payload,
                        "requires_watchlist_maintenance",
                        path=shard_path,
                    ),
                    caused_analysis_request=payload_caused_analysis_request,
                    validator_action=_require_text(
                        payload,
                        "validator_action",
                        path=shard_path,
                    ),
                    decision_episode_id=_optional_text(payload, "decision_episode_id"),
                    path=shard_path,
                    line_number=line_number,
                )
            )
    return receipts


def _find_latest_attention_receipt(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
) -> CheckerDecisionReceipt | None:
    records = FileBackedDecisionEpisodeStore(layout).read_records(target_key=target_key)
    matches: list[CheckerDecisionReceipt] = []
    for persisted in records:
        record = persisted.record
        if record.record_type != "attention":
            continue
        if event_id not in record.event_ids:
            continue
        payload_decision = record.payload.get("decision")
        if payload_decision != "escalate":
            continue
        rationale = record.payload.get("rationale")
        attention_hint = record.payload.get("attention_hint")
        requires_watchlist_maintenance = record.payload.get(
            "requires_watchlist_maintenance"
        )
        if not isinstance(rationale, str) or not rationale.strip():
            continue
        if not isinstance(attention_hint, str) or not attention_hint.strip():
            continue
        if not isinstance(requires_watchlist_maintenance, bool):
            continue
        matches.append(
            CheckerDecisionReceipt(
                target_key=record.target_key,
                event_id=event_id,
                source_ref="",
                title="",
                business_at=record.business_at,
                decision="escalate",
                rationale=rationale.strip(),
                attention_hint=attention_hint.strip(),
                requires_watchlist_maintenance=requires_watchlist_maintenance,
                caused_analysis_request=False,
                validator_action="accepted",
                decision_episode_id=record.episode_id,
                path=persisted.path,
                line_number=persisted.line_number,
            )
        )
    if not matches:
        return None
    return matches[-1]


def _load_json_object(
    raw_text: str,
    *,
    path: Path,
    line_number: int,
) -> dict[str, object]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CheckerReceiptStoreError(
            f"invalid checker decision JSON at {path}:{line_number}"
        ) from exc
    if not isinstance(payload, dict):
        raise CheckerReceiptStoreError(
            f"checker decision payload must be an object at {path}:{line_number}"
        )
    return payload


def _require_target_key(
    payload: dict[str, object],
    field_name: str,
    *,
    path: Path,
) -> str:
    return validate_target_key(
        _require_text(payload, field_name, path=path),
        error_type=CheckerReceiptStoreError,
    )


def _require_event_id(
    payload: dict[str, object],
    field_name: str,
    *,
    path: Path,
) -> str:
    return validate_event_id(
        _require_text(payload, field_name, path=path),
        error_type=CheckerReceiptStoreError,
    )


def _require_text(
    payload: dict[str, object],
    field_name: str,
    *,
    path: Path,
) -> str:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CheckerReceiptStoreError(
            f"{field_name} must be a non-blank string at {path}"
        )
    return raw_value.strip()


def _optional_text(payload: dict[str, object], field_name: str) -> str:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, str):
        return ""
    return raw_value.strip()


def _require_bool(
    payload: dict[str, object],
    field_name: str,
    *,
    path: Path,
) -> bool:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, bool):
        raise CheckerReceiptStoreError(f"{field_name} must be boolean at {path}")
    return raw_value


def _require_datetime(
    payload: dict[str, object],
    field_name: str,
    *,
    path: Path,
) -> datetime:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CheckerReceiptStoreError(
            f"{field_name} must be a non-blank ISO8601 string at {path}"
        )
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckerReceiptStoreError(
            f"{field_name} must be a valid ISO8601 string at {path}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CheckerReceiptStoreError(
            f"{field_name} must be timezone-aware at {path}"
        )
    return parsed


__all__ = [
    "CheckerDecisionReceipt",
    "CheckerReceiptStoreError",
    "find_latest_escalation_reuse_receipt",
    "find_latest_checker_decision_receipt",
    "iter_checker_decision_receipts",
]
