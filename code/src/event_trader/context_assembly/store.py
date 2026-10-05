"""File-backed persistence for auditable context packets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from event_trader.context_assembly.packets import (
    ContextPacket,
    ContextPacketRuntimeScope,
    VisibilityAudit,
    hash_context_packet,
    parse_context_packet_payload,
    validate_context_packet_visibility,
)
from event_trader.storage import WorkspaceLayout

_CONTEXT_PACKET_DIR_NAME = "context_packets"


class ContextPacketStoreError(ValueError):
    """Raised when persisted context packets are invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedContextPacket:
    packet: ContextPacket
    visibility_audit: dict[str, object]


class FileBackedContextPacketStore:
    """Append full context packets with idempotent packet_id semantics."""

    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        runtime_scope: ContextPacketRuntimeScope = "live",
        run_id: str = "",
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ContextPacketStoreError("layout must be a WorkspaceLayout instance.")
        if runtime_scope not in {"live", "replay"}:
            raise ContextPacketStoreError("runtime_scope must be live or replay.")
        normalized_run_id = run_id.strip() if isinstance(run_id, str) else ""
        if runtime_scope == "live":
            if normalized_run_id:
                raise ContextPacketStoreError("live packet store must not carry run_id.")
        elif not normalized_run_id:
            raise ContextPacketStoreError("replay packet store requires run_id.")
        self._layout = layout
        self._runtime_scope = runtime_scope
        self._run_id = normalized_run_id

    def append_packet(
        self,
        packet: ContextPacket,
        *,
        required_memory_read_policy: str | None = None,
        precomputed_visibility_audit: VisibilityAudit | None = None,
    ) -> Path:
        if not isinstance(packet, ContextPacket):
            raise ContextPacketStoreError("packet must be a ContextPacket instance.")
        if packet.runtime_scope != self._runtime_scope:
            raise ContextPacketStoreError("packet runtime_scope does not match store.")
        if packet.run_id != self._run_id:
            raise ContextPacketStoreError("packet run_id does not match store.")
        if packet.packet_hash != hash_context_packet(packet):
            raise ContextPacketStoreError("context packet hash does not match payload.")
        visibility_audit = precomputed_visibility_audit or validate_context_packet_visibility(
            packet,
            required_memory_read_policy=required_memory_read_policy,
            required_runtime_scope=self._runtime_scope,
            required_run_id=(self._run_id if self._runtime_scope == "replay" else None),
        )
        path = self.packet_path(
            stage=packet.stage,
            target_key=packet.target_key,
            business_at=packet.business_at,
        )
        existing = self.read_packets(
            stage=packet.stage,
            target_key=packet.target_key,
            year_month=packet.business_at.strftime("%Y-%m"),
        )
        for persisted in existing:
            if persisted.packet.packet_id != packet.packet_id:
                continue
            if persisted.packet.packet_hash == packet.packet_hash:
                return path
            raise ContextPacketStoreError(
                "context packet duplicate packet_id has different packet_hash."
            )
        payload = {
            **packet.to_json_payload(),
            "visibility_audit": visibility_audit.to_json_payload(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        except OSError as exc:
            raise ContextPacketStoreError(
                f"Failed to append context packet {packet.packet_id!r}: {exc}"
            ) from exc
        return path

    def read_packets(
        self,
        *,
        stage: str,
        target_key: str,
        year_month: str,
    ) -> tuple[PersistedContextPacket, ...]:
        path = self._packet_path_for_month(
            stage=stage,
            target_key=target_key,
            year_month=year_month,
        )
        if not path.exists():
            return ()
        if not path.is_file():
            raise ContextPacketStoreError(f"context packet path must be a file: {path}")
        packets: list[PersistedContextPacket] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise ContextPacketStoreError(
                        f"context packet line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, dict):
                    raise ContextPacketStoreError(
                        f"context packet line {line_number} must be a JSON object."
                    )
                visibility_audit = payload.get("visibility_audit")
                if not isinstance(visibility_audit, dict):
                    raise ContextPacketStoreError(
                        f"context packet line {line_number} missing visibility_audit."
                    )
                packets.append(
                    PersistedContextPacket(
                        packet=parse_context_packet_payload(payload),
                        visibility_audit=dict(visibility_audit),
                    )
                )
        return tuple(packets)

    def read_all_packets(self) -> tuple[PersistedContextPacket, ...]:
        root = self._store_root()
        if not root.exists():
            return ()
        packets: list[PersistedContextPacket] = []
        for path in sorted(root.glob("*/*/*.jsonl")):
            stage = path.parent.parent.name
            target_key = path.parent.name
            year_month = path.stem
            packets.extend(
                self.read_packets(
                    stage=stage,
                    target_key=target_key,
                    year_month=year_month,
                )
            )
        return tuple(packets)

    def packet_path(
        self,
        *,
        stage: str,
        target_key: str,
        business_at,
    ) -> Path:
        return self._packet_path_for_month(
            stage=stage,
            target_key=target_key,
            year_month=business_at.strftime("%Y-%m"),
        )

    def _packet_path_for_month(
        self,
        *,
        stage: str,
        target_key: str,
        year_month: str,
    ) -> Path:
        if stage not in {"checker", "analysis"}:
            raise ContextPacketStoreError("stage must be checker or analysis.")
        if not isinstance(target_key, str) or not target_key.strip():
            raise ContextPacketStoreError("target_key must be non-blank.")
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise ContextPacketStoreError("year_month must be YYYY-MM.")
        return (
            self._store_root()
            / stage
            / target_key.strip()
            / f"{year_month}.jsonl"
        ).resolve(strict=False)

    def _store_root(self) -> Path:
        root = self._layout.runtime_root / _CONTEXT_PACKET_DIR_NAME / self._runtime_scope
        if self._runtime_scope == "replay":
            return root / self._run_id
        return root

__all__ = [
    "ContextPacketStoreError",
    "FileBackedContextPacketStore",
    "PersistedContextPacket",
]
