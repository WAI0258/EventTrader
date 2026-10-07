"""Local read-only PM Console API server."""

from __future__ import annotations

import argparse
import json
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, unquote, urlparse

from event_trader.config import (
    BootstrapConfigError,
    CheckerAgentConfig,
    ExecutionConfig,
    load_execution_config_for_workspace,
    load_kernel_config,
    load_workspace_execution_config,
    resolve_execution_buy_cost_bps_for_target,
)
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.market.shared_store import (
    read_workspace_snapshot_ref,
    shared_market_data_root,
)
from event_trader.operator_context import (
    OperatorContextConflictError,
    OperatorContextService,
    OperatorContextServiceError,
)
from event_trader.pm_console import (
    OpenAICompatibleThesisQuickBriefGenerator,
    OpenAICompatibleThesisSectionTranslator,
    PMConsoleSchemaError,
    PMConsoleThesisQuickBriefError,
    PMConsoleThesisQuickBriefRequestDTO,
    PMConsoleThesisQuickBriefService,
    PMConsoleThesisTranslationFailure,
    PMConsoleThesisTranslationRequestDTO,
    PMConsoleThesisTranslationService,
    build_analysis_assessment_detail,
    build_context_response,
    build_episode_detail_response,
    build_episodes_response,
    build_overview_response,
    build_overview_target,
    build_pm_decision_detail_response,
    build_pm_decisions_response,
    build_position_response,
    build_targets_response,
    build_thesis_revision_detail_response,
    build_thesis_revisions_response,
    build_unavailable_overview_target,
)
from event_trader.pm_console.schemas import PMConsoleTargetDTO, PMConsoleTargetsResponseDTO
from event_trader.pm_console.thesis import (
    default_pm_console_target_view,
    list_pm_console_targets,
    map_pm_console_target_views,
)
from event_trader.position_monitoring import (
    build_position_monitoring_snapshot,
    list_position_monitoring_targets,
)
from event_trader.storage import WorkspaceLayout, build_workspace_layout
from event_trader.tools.pm_console_buy_hold import apply_overlay
from event_trader.tools.pm_console_mounts import PMConsoleMountRegistry, PMConsoleMountSnapshot
from event_trader.validation.market_mapping import resolve_market_mapping


class PMConsoleServerError(ValueError):
    """Raised when the PM Console server cannot start or serve a request."""


@dataclass(frozen=True, slots=True)
class PMConsoleBinding:
    """All read-model dependencies for one isolated PM Console target."""

    target_key: str
    layout: WorkspaceLayout
    runtime_mode: str
    market_data_snapshot_id: str | None
    display_start_at: datetime | None
    execution_config: ExecutionConfig | None
    reader_config: CheckerAgentConfig | None
    thesis_translation_service: PMConsoleThesisTranslationService
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService
    config_path: Path | None = None
    market_data_root: Path | None = None
    market_data_mapping: MarketMapping | None = None
    market_data_provider_name: str | None = None


class _PositionMarketDataKwargs(TypedDict):
    market_data_root: Path | None
    market_data_snapshot_id: str | None
    market_data_provider_name: str | None
    market_mapping: MarketMapping | None


def _build_binding(
    *,
    target_key: str,
    workspace_root: Path,
    config_path: Path | None,
    runtime_mode: str,
    market_data_snapshot_id: str | None,
    display_start_at: datetime | None,
) -> PMConsoleBinding:
    if not workspace_root.exists() or not workspace_root.is_dir():
        raise PMConsoleServerError(f"workspace root does not exist: {workspace_root}")
    layout = build_workspace_layout(workspace_root)
    resolved_config_path = _resolve_config_path(
        config_path=config_path,
        workspace_root=layout.root,
        runtime_mode=runtime_mode,
    )
    execution_config = _load_execution_config(
        config_path=resolved_config_path,
        workspace_root=layout.root,
    )
    binding_runtime_mode = runtime_mode
    market_data_root = None
    resolved_snapshot_id = market_data_snapshot_id
    market_data_mapping = None
    market_data_provider_name = None
    if resolved_config_path is not None:
        try:
            kernel_config = load_kernel_config(resolved_config_path)
        except BootstrapConfigError as exc:
            raise PMConsoleServerError(
                f"could not load config for target {target_key!r}: {exc}"
            ) from exc
        if kernel_config.workspace_root != layout.root:
            raise PMConsoleServerError(
                f"config workspace_root does not match target {target_key!r}: "
                f"config={kernel_config.workspace_root} requested={layout.root}"
            )
        if kernel_config.stream_routing is None:
            raise PMConsoleServerError(
                f"config for target {target_key!r} must define stream_routing."
            )
        if kernel_config.stream_routing.target_key != target_key:
            raise PMConsoleServerError(
                f"config stream_routing.target_key does not match target {target_key!r}: "
                f"{kernel_config.stream_routing.target_key!r}"
            )
        binding_runtime_mode = kernel_config.mode
        market_data_root = shared_market_data_root(kernel_config)
        if kernel_config.validation is not None:
            market_data_provider_name = kernel_config.validation.market_data.provider
            try:
                market_data_mapping = resolve_market_mapping(kernel_config, target_key)
            except Exception:
                market_data_mapping = None
        if binding_runtime_mode == "replay" and resolved_snapshot_id is None:
            resolved_snapshot_id = read_workspace_snapshot_ref(
                layout.root,
                target_key=target_key,
                run_id=None,
            )
    reader_config = _resolve_reader_config(config_path=resolved_config_path)
    return PMConsoleBinding(
        target_key=target_key,
        layout=layout,
        runtime_mode=binding_runtime_mode,
        market_data_snapshot_id=resolved_snapshot_id,
        display_start_at=display_start_at,
        execution_config=execution_config,
        reader_config=reader_config,
        thesis_translation_service=_build_thesis_translation_service(
            layout=layout,
            reader_config=reader_config,
        ),
        thesis_quick_brief_service=_build_thesis_quick_brief_service(
            layout=layout,
            reader_config=reader_config,
        ),
        config_path=resolved_config_path,
        market_data_root=market_data_root,
        market_data_mapping=market_data_mapping,
        market_data_provider_name=market_data_provider_name,
    )


def _build_binding_from_config(
    config_path: Path,
    *,
    market_data_snapshot_id: str | None,
    display_start_at: datetime | None,
) -> PMConsoleBinding:
    """Build one binding solely from the config's validated identity."""

    try:
        kernel_config = load_kernel_config(config_path)
    except BootstrapConfigError as exc:
        raise PMConsoleServerError(f"could not load mount config {config_path}: {exc}") from exc
    if kernel_config.stream_routing is None:
        raise PMConsoleServerError(f"config must define stream_routing: {config_path}")
    return _build_binding(
        target_key=kernel_config.stream_routing.target_key,
        workspace_root=kernel_config.workspace_root,
        config_path=config_path,
        runtime_mode=kernel_config.mode,
        market_data_snapshot_id=market_data_snapshot_id,
        display_start_at=display_start_at,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    host = args.host.strip()
    if host not in {"127.0.0.1", "localhost"} and not args.allow_non_loopback:
        raise SystemExit(
            "error: non-loopback hosts require --allow-non-loopback for the local "
            "PM Console server."
        )
    mount_registry: PMConsoleMountRegistry | None = None
    if args.mount_catalog is not None:
        if args.workspace_root is not None or args.config is not None:
            raise SystemExit(
                "error: --mount-catalog cannot be combined with --workspace-root/--config."
            )
        mount_registry = PMConsoleMountRegistry(
            args.mount_catalog,
            build_binding=lambda config_path: _build_binding_from_config(
                config_path,
                market_data_snapshot_id=args.market_data_snapshot_id,
                display_start_at=args.display_start_at,
            ),
        )
        initial_snapshot = mount_registry.snapshot()
        layout = (
            next(iter(initial_snapshot.bindings.values())).layout
            if initial_snapshot.bindings
            else build_workspace_layout(Path(args.mount_catalog).expanduser().resolve().parent)
        )
    else:
        if args.workspace_root is None:
            raise SystemExit("error: provide --workspace-root or --mount-catalog.")
        workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise SystemExit(f"error: workspace root does not exist: {workspace_root}")
        layout = build_workspace_layout(workspace_root)
        config_path = _resolve_config_path(
            config_path=args.config,
            workspace_root=layout.root,
            runtime_mode=args.runtime_mode,
        )
        execution_config = _load_execution_config(
            config_path=config_path,
            workspace_root=layout.root,
        )
        reader_config = _resolve_reader_config(config_path=config_path)
        thesis_translation_service = _build_thesis_translation_service(
            layout=layout,
            reader_config=reader_config,
        )
        thesis_quick_brief_service = _build_thesis_quick_brief_service(
            layout=layout,
            reader_config=reader_config,
        )
    server = ThreadingHTTPServer(
        (host, args.port),
        _build_handler(
            layout,
            runtime_mode=args.runtime_mode,
            market_data_snapshot_id=args.market_data_snapshot_id,
            display_start_at=args.display_start_at,
            execution_config=(None if mount_registry is not None else execution_config),
            reader_config=(None if mount_registry is not None else reader_config),
            thesis_translation_service=(
                None if mount_registry is not None else thesis_translation_service
            ),
            thesis_quick_brief_service=(
                None if mount_registry is not None else thesis_quick_brief_service
            ),
            bindings=None,
            mount_registry=mount_registry,
            saved_results=args.saved_results,
            buy_hold_results_dir=args.buy_hold_results_dir,
        ),
    )
    try:
        print(
            f"PM Console server listening on http://{host}:{server.server_port} "
            + (
                f"workspace_root={layout.root}"
                if mount_registry is None
                else f"mount_catalog={mount_registry.catalog_path}"
            )
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _build_handler(
    layout: WorkspaceLayout,
    *,
    runtime_mode: str,
    market_data_snapshot_id: str | None,
    display_start_at: datetime | None,
    execution_config: ExecutionConfig | None = None,
    reader_config: CheckerAgentConfig | None = None,
    thesis_translation_service: PMConsoleThesisTranslationService | None = None,
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService | None = None,
    bindings: Mapping[str, PMConsoleBinding] | None = None,
    mount_registry: PMConsoleMountRegistry | None = None,
    saved_results: bool = False,
    buy_hold_results_dir: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    translation_service = thesis_translation_service or _build_thesis_translation_service(
        layout=layout,
        reader_config=reader_config,
    )
    quick_brief_service = thesis_quick_brief_service or _build_thesis_quick_brief_service(
        layout=layout,
        reader_config=reader_config,
    )

    class PMConsoleHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            _write_cors_headers(self)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            try:
                request_snapshot = mount_registry.snapshot() if mount_registry is not None else None
                saved_target = _target_from_position_path(urlparse(self.path).path)
                if saved_results and saved_target is not None:
                    active_bindings = (
                        request_snapshot.bindings if request_snapshot is not None else bindings
                    )
                    binding = None if active_bindings is None else active_bindings.get(saved_target)
                    if active_bindings is not None and binding is None:
                        status, payload = HTTPStatus.NOT_FOUND, {"error": "unknown target"}
                    else:
                        sample_layout = layout if binding is None else binding.layout
                        if saved_target not in list_pm_console_targets(sample_layout):
                            raise PMConsoleServerError("Unknown saved-result target.")
                        sample_path = (
                            sample_layout.runtime_root
                            / "pm_console"
                            / f"{saved_target}.position.json"
                        )
                        if not sample_path.is_file():
                            raise PMConsoleServerError(
                                "Saved position results are missing for this target."
                            )
                        sample_bytes = sample_path.read_bytes()
                        payload = json.loads(sample_bytes)
                        if (
                            not isinstance(payload, dict)
                            or payload.get("target_key") != saved_target
                        ):
                            raise PMConsoleServerError(
                                "Saved position results have an invalid target identity."
                            )
                        if buy_hold_results_dir is not None:
                            overlay_path = buy_hold_results_dir / f"{saved_target}.buy-hold.json"
                            if overlay_path.is_file():
                                try:
                                    payload = apply_overlay(
                                        payload=payload, sample=sample_bytes,
                                        overlay=json.loads(overlay_path.read_bytes()),
                                    )
                                except (ValueError, KeyError, TypeError) as exc:
                                    raise PMConsoleServerError(str(exc)) from exc
                        status = HTTPStatus.OK
                else:
                    status, payload = _dispatch_get(
                        layout=layout,
                        raw_path=self.path,
                        runtime_mode=runtime_mode,
                        market_data_snapshot_id=market_data_snapshot_id,
                        display_start_at=display_start_at,
                        execution_config=execution_config,
                        translation_service=translation_service,
                        thesis_quick_brief_service=quick_brief_service,
                        bindings=(
                            request_snapshot.bindings if request_snapshot is not None else bindings
                        ),
                        mount_snapshot=request_snapshot,
                    )
            except PMConsoleServerError as exc:
                status = HTTPStatus.BAD_REQUEST
                payload = {"error": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive API boundary
                traceback.print_exc()
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = {"error": str(exc)}
            self.send_response(status)
            _write_cors_headers(self)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )

        def do_POST(self) -> None:  # noqa: N802
            if saved_results:
                self.send_error(
                    HTTPStatus.METHOD_NOT_ALLOWED, "Saved-result samples are read-only."
                )
                return
            try:
                request_snapshot = mount_registry.snapshot() if mount_registry is not None else None
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                status, payload = _dispatch_post(
                    layout=layout,
                    raw_path=self.path,
                    raw_body=raw_body,
                    translation_service=translation_service,
                    thesis_quick_brief_service=quick_brief_service,
                    bindings=(
                        request_snapshot.bindings if request_snapshot is not None else bindings
                    ),
                )
            except PMConsoleThesisTranslationFailure as exc:
                status = exc.status
                payload = exc.response.to_json_payload()
            except PMConsoleThesisQuickBriefError as exc:
                status = exc.status
                payload = exc.response.to_json_payload()
            except OperatorContextConflictError as exc:
                status = HTTPStatus.CONFLICT
                payload = _stale_operator_context_payload(exc)
            except OperatorContextServiceError as exc:
                status = HTTPStatus.BAD_REQUEST
                payload = {"error": str(exc)}
            except PMConsoleServerError as exc:
                status = HTTPStatus.BAD_REQUEST
                payload = {"error": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive API boundary
                traceback.print_exc()
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = {"error": str(exc)}
            self.send_response(status)
            _write_cors_headers(self)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )

        def do_PUT(self) -> None:  # noqa: N802
            if saved_results:
                self.send_error(
                    HTTPStatus.METHOD_NOT_ALLOWED, "Saved-result samples are read-only."
                )
                return
            try:
                request_snapshot = mount_registry.snapshot() if mount_registry is not None else None
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                status, payload = _dispatch_put(
                    layout=layout,
                    raw_path=self.path,
                    raw_body=raw_body,
                    bindings=(
                        request_snapshot.bindings if request_snapshot is not None else bindings
                    ),
                )
            except OperatorContextConflictError as exc:
                status = HTTPStatus.CONFLICT
                payload = _stale_operator_context_payload(exc)
            except OperatorContextServiceError as exc:
                status = HTTPStatus.BAD_REQUEST
                payload = {"error": str(exc)}
            except PMConsoleServerError as exc:
                status = HTTPStatus.BAD_REQUEST
                payload = {"error": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive API boundary
                traceback.print_exc()
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = {"error": str(exc)}
            self.send_response(status)
            _write_cors_headers(self)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return None

    return PMConsoleHandler


def _build_targets_response_for_bindings(
    bindings: Mapping[str, PMConsoleBinding],
    *,
    mount_snapshot: PMConsoleMountSnapshot | None = None,
) -> dict[str, object]:
    generated_at = datetime.now(UTC).isoformat()
    targets: list[PMConsoleTargetDTO] = []
    for target_key in sorted(bindings):
        views = map_pm_console_target_views(bindings[target_key].layout).get(
            target_key,
            ("operator",),
        )
        targets.append(
            PMConsoleTargetDTO(
                target_key=target_key,
                implemented_views=views,
                default_view=default_pm_console_target_view(views),
            )
        )
    payload = PMConsoleTargetsResponseDTO(
        generated_at=generated_at,
        targets=tuple(targets),
    ).to_json_payload()
    if mount_snapshot is not None:
        payload["generation"] = mount_snapshot.generation
        payload["reload_error"] = mount_snapshot.reload_error
    return payload


def _build_overview_response_for_bindings(
    bindings: Mapping[str, PMConsoleBinding],
    *,
    mount_snapshot: PMConsoleMountSnapshot | None = None,
) -> dict[str, object]:
    rows = []
    for target_key in sorted(bindings):
        binding = bindings[target_key]
        try:
            rows.append(
                build_overview_target(
                    binding.layout,
                    target_key=target_key,
                    runtime_mode=binding.runtime_mode,
                )
            )
        except Exception as exc:  # one malformed workspace must not break the board
            rows.append(
                build_unavailable_overview_target(
                    target_key=target_key,
                    layout=binding.layout,
                    runtime_mode=binding.runtime_mode,
                    error=exc,
                )
            )
    payload = build_overview_response(
        rows,
        mounted_target_count=len(bindings),
        reload_error=None if mount_snapshot is None else mount_snapshot.reload_error,
    ).to_json_payload()
    if mount_snapshot is not None:
        payload["generation"] = mount_snapshot.generation
    return payload


def _resolve_target_scope(
    *,
    target: str,
    bindings: Mapping[str, PMConsoleBinding] | None,
    layout: WorkspaceLayout,
    runtime_mode: str,
    market_data_snapshot_id: str | None,
    display_start_at: datetime | None,
    execution_config: ExecutionConfig | None,
    translation_service: PMConsoleThesisTranslationService | None,
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService | None,
    known_targets: set[str],
) -> (
    tuple[
        WorkspaceLayout,
        str,
        str | None,
        datetime | None,
        ExecutionConfig | None,
        PMConsoleThesisTranslationService | None,
        PMConsoleThesisQuickBriefService | None,
    ]
    | None
):
    if bindings is not None:
        binding = bindings.get(target)
        if binding is None:
            return None
        return (
            binding.layout,
            binding.runtime_mode,
            binding.market_data_snapshot_id,
            binding.display_start_at,
            binding.execution_config,
            binding.thesis_translation_service,
            binding.thesis_quick_brief_service,
        )
    if target not in known_targets:
        return None
    return (
        layout,
        runtime_mode,
        market_data_snapshot_id,
        display_start_at,
        execution_config,
        translation_service,
        thesis_quick_brief_service,
    )


def _dispatch_get(
    *,
    layout: WorkspaceLayout,
    raw_path: str,
    runtime_mode: str = "unknown",
    market_data_snapshot_id: str | None = None,
    display_start_at: datetime | None = None,
    execution_config: ExecutionConfig | None = None,
    translation_service: PMConsoleThesisTranslationService | None = None,
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService | None = None,
    bindings: Mapping[str, PMConsoleBinding] | None = None,
    mount_snapshot: PMConsoleMountSnapshot | None = None,
) -> tuple[HTTPStatus, dict[str, object]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    if path == "/api/context":
        if bindings is not None:
            return HTTPStatus.BAD_REQUEST, {
                "error": "aggregate mode requires /api/targets/{target}/context."
            }
        return (
            HTTPStatus.OK,
            build_context_response(
                layout,
                runtime_mode=runtime_mode,
                thesis_translation_service=translation_service,
            ).to_json_payload(),
        )
    if path == "/api/overview":
        if bindings is not None:
            return HTTPStatus.OK, _build_overview_response_for_bindings(
                bindings,
                mount_snapshot=mount_snapshot,
            )
        rows = tuple(
            build_overview_target(
                layout,
                target_key=target_key,
                runtime_mode=runtime_mode,
            )
            for target_key in list_pm_console_targets(layout)
        )
        return HTTPStatus.OK, build_overview_response(
            rows,
            mounted_target_count=len(rows),
        ).to_json_payload()
    if path == "/api/targets":
        if bindings is not None:
            return HTTPStatus.OK, _build_targets_response_for_bindings(
                bindings,
                mount_snapshot=mount_snapshot,
            )
        return HTTPStatus.OK, build_targets_response(layout).to_json_payload()
    episode_detail = _target_and_episode_detail_path(path)
    if episode_detail is not None:
        target, episode_id = episode_detail
        scope = _resolve_target_scope(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        payload = build_episode_detail_response(scope[0], target_key=target, episode_id=episode_id)
        if payload is None:
            return HTTPStatus.NOT_FOUND, {
                "error": f"unknown position episode: target={target} episode_id={episode_id}"
            }
        return HTTPStatus.OK, payload.to_json_payload()
    episode_target = _target_from_episodes_path(path)
    if episode_target is not None:
        scope = _resolve_target_scope(
            target=episode_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {episode_target}"}
        try:
            payload = build_episodes_response(
                scope[0],
                target_key=episode_target,
                offset=_query_int(parsed.query, "offset", 0),
                limit=_query_int(parsed.query, "limit", 50),
                phase=_query_value(parsed.query, "phase"),
                query=_query_value(parsed.query, "q"),
                anchor_episode_id=_query_value(parsed.query, "anchor_episode_id"),
            )
        except ValueError as exc:
            raise PMConsoleServerError(str(exc)) from exc
        return HTTPStatus.OK, payload.to_json_payload()
    operator_history_detail = _target_and_operator_history_version_path(path)
    if operator_history_detail is not None:
        target, version_id = operator_history_detail
        target_layout = _operator_target_layout(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
        )
        if target_layout is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        try:
            version = OperatorContextService(target_layout).read_history(target, version_id)
        except (FileNotFoundError, OperatorContextServiceError):
            return HTTPStatus.NOT_FOUND, {
                "error": (
                    f"unknown operator history version: target={target} version_id={version_id}"
                )
            }
        return HTTPStatus.OK, version.to_json_payload(include_content=True)
    operator_history_target = _target_from_operator_history_path(path)
    if operator_history_target is not None:
        target_layout = _operator_target_layout(
            target=operator_history_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
        )
        if target_layout is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {operator_history_target}"}
        service = OperatorContextService(target_layout)
        return HTTPStatus.OK, {
            "target_key": operator_history_target,
            "versions": [
                version.to_json_payload(include_content=False)
                for version in service.list_history(operator_history_target)
            ],
        }
    operator_target = _target_from_operator_path(path)
    if operator_target is not None:
        target_layout = _operator_target_layout(
            target=operator_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
        )
        if target_layout is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {operator_target}"}
        document = OperatorContextService(target_layout).read(operator_target)
        return HTTPStatus.OK, document.to_json_payload()
    context_target = _target_from_context_path(path)
    if context_target is not None:
        scope = _resolve_target_scope(
            target=context_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {context_target}"}
        target_layout, target_runtime_mode, _, _, _, target_translation, _ = scope
        return (
            HTTPStatus.OK,
            build_context_response(
                target_layout,
                runtime_mode=target_runtime_mode,
                thesis_translation_service=target_translation,
            ).to_json_payload(),
        )
    thesis_target = _target_from_thesis_revisions_path(path)
    if thesis_target is not None:
        scope = _resolve_target_scope(
            target=thesis_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {thesis_target}"}
        target_layout = scope[0]
        return (
            HTTPStatus.OK,
            build_thesis_revisions_response(
                target_layout,
                target_key=thesis_target,
            ).to_json_payload(),
        )
    thesis_detail = _target_and_revision_from_thesis_detail_path(path)
    if thesis_detail is not None:
        target, revision_id = thesis_detail
        scope = _resolve_target_scope(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        target_layout = scope[0]
        payload = build_thesis_revision_detail_response(
            target_layout,
            target_key=target,
            revision_id=revision_id,
        )
        if payload is None:
            return (
                HTTPStatus.NOT_FOUND,
                {"error": (f"unknown thesis revision: target={target} revision_id={revision_id}")},
            )
        return HTTPStatus.OK, payload.to_json_payload()
    pm_target = _target_from_pm_decisions_path(path)
    if pm_target is not None:
        scope = _resolve_target_scope(
            target=pm_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {pm_target}"}
        target_layout = scope[0]
        return (
            HTTPStatus.OK,
            build_pm_decisions_response(target_layout, target_key=pm_target).to_json_payload(),
        )
    pm_detail = _target_and_decision_from_pm_path(path)
    if pm_detail is not None:
        target, decision_id = pm_detail
        scope = _resolve_target_scope(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        target_layout = scope[0]
        pm_detail_payload = build_pm_decision_detail_response(
            target_layout,
            target_key=target,
            decision_id=decision_id,
        )
        if pm_detail_payload is None:
            return (
                HTTPStatus.NOT_FOUND,
                {"error": f"unknown PM decision: target={target} decision_id={decision_id}"},
            )
        return HTTPStatus.OK, pm_detail_payload.to_json_payload()
    analysis_detail = _target_and_assessment_from_analysis_path(path)
    if analysis_detail is not None:
        target, assessment_id = analysis_detail
        scope = _resolve_target_scope(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        target_layout = scope[0]
        analysis_detail_payload = build_analysis_assessment_detail(
            target_layout,
            target_key=target,
            assessment_id=assessment_id,
        )
        if analysis_detail_payload is None:
            return (
                HTTPStatus.NOT_FOUND,
                {
                    "error": (
                        f"unknown AnalysisAssessment: target={target} assessment_id={assessment_id}"
                    )
                },
            )
        return HTTPStatus.OK, analysis_detail_payload.to_json_payload()
    position_target = _target_from_position_path(path)
    if position_target is not None:
        scope = _resolve_target_scope(
            target=position_target,
            bindings=bindings,
            layout=layout,
            runtime_mode=runtime_mode,
            market_data_snapshot_id=market_data_snapshot_id,
            display_start_at=display_start_at,
            execution_config=execution_config,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_position_monitoring_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {position_target}"}
        (
            target_layout,
            target_runtime_mode,
            target_market_data_snapshot_id,
            target_display_start_at,
            target_execution_config,
            _,
            _,
        ) = scope
        return (
            HTTPStatus.OK,
            _build_position_response_for_target(
                layout=target_layout,
                target=position_target,
                runtime_mode=target_runtime_mode,
                display_start_at=target_display_start_at,
                execution_config=target_execution_config,
                **_position_market_data_kwargs(
                    bindings=bindings,
                    target=position_target,
                    layout=target_layout,
                    market_data_snapshot_id=target_market_data_snapshot_id,
                ),
            ).to_json_payload(),
        )
    return HTTPStatus.NOT_FOUND, {"error": f"unknown route: {path}"}


def _dispatch_post(
    *,
    layout: WorkspaceLayout,
    raw_path: str,
    raw_body: bytes,
    translation_service: PMConsoleThesisTranslationService | None = None,
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService | None = None,
    bindings: Mapping[str, PMConsoleBinding] | None = None,
) -> tuple[HTTPStatus, dict[str, object]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    operator_restore = _target_and_operator_restore_path(path)
    if operator_restore is not None:
        target, version_id = operator_restore
        target_layout = _operator_target_layout(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode="unknown",
            market_data_snapshot_id=None,
            display_start_at=None,
            execution_config=None,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
        )
        if target_layout is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        request = _parse_operator_restore_request_payload(raw_body)
        try:
            document = OperatorContextService(target_layout).restore(
                target,
                version_id=version_id,
                expected_content_sha256=request["expected_content_sha256"],
            )
        except FileNotFoundError:
            return HTTPStatus.NOT_FOUND, {
                "error": (
                    f"unknown operator history version: target={target} version_id={version_id}"
                )
            }
        except OperatorContextConflictError as exc:
            return HTTPStatus.CONFLICT, _stale_operator_context_payload(exc)
        return HTTPStatus.OK, document.to_json_payload()
    brief_detail = _target_and_revision_from_brief_path(path)
    if brief_detail is not None:
        target, revision_id = brief_detail
        scope = _resolve_target_scope(
            target=target,
            bindings=bindings,
            layout=layout,
            runtime_mode="unknown",
            market_data_snapshot_id=None,
            display_start_at=None,
            execution_config=None,
            translation_service=translation_service,
            thesis_quick_brief_service=thesis_quick_brief_service,
            known_targets=set(list_pm_console_targets(layout)),
        )
        if scope is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        _, _, _, _, _, _, target_quick_brief_service = scope
        if target_quick_brief_service is None:
            raise PMConsoleServerError("quick brief service is unavailable.")
        brief_request = _parse_quick_brief_request_payload(raw_body)
        brief_payload = target_quick_brief_service.build_brief(
            target_key=target,
            revision_id=revision_id,
            request=brief_request,
        )
        return HTTPStatus.OK, brief_payload.to_json_payload()
    translation_detail = _target_and_revision_from_translate_path(path)
    if translation_detail is None:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown route: {path}"}
    target, revision_id = translation_detail
    scope = _resolve_target_scope(
        target=target,
        bindings=bindings,
        layout=layout,
        runtime_mode="unknown",
        market_data_snapshot_id=None,
        display_start_at=None,
        execution_config=None,
        translation_service=translation_service,
        thesis_quick_brief_service=thesis_quick_brief_service,
        known_targets=set(list_pm_console_targets(layout)),
    )
    if scope is None:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
    _, _, _, _, _, target_translation_service, _ = scope
    if target_translation_service is None:
        raise PMConsoleServerError("thesis translation service is unavailable.")
    translation_request = _parse_translation_request_payload(raw_body)
    translation_payload = target_translation_service.translate_revision(
        target_key=target,
        revision_id=revision_id,
        request=translation_request,
    )
    return HTTPStatus.OK, translation_payload.to_json_payload()


def _dispatch_put(
    *,
    layout: WorkspaceLayout,
    raw_path: str,
    raw_body: bytes,
    bindings: Mapping[str, PMConsoleBinding] | None = None,
) -> tuple[HTTPStatus, dict[str, object]]:
    path = urlparse(raw_path).path.rstrip("/") or "/"
    target = _target_from_operator_path(path)
    if target is None:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown route: {path}"}
    target_layout = _operator_target_layout(
        target=target,
        bindings=bindings,
        layout=layout,
        runtime_mode="unknown",
        market_data_snapshot_id=None,
        display_start_at=None,
        execution_config=None,
        translation_service=None,
        thesis_quick_brief_service=None,
    )
    if target_layout is None:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
    request = _parse_operator_put_request_payload(raw_body)
    try:
        document = OperatorContextService(target_layout).replace(target, **request)
    except OperatorContextConflictError as exc:
        return HTTPStatus.CONFLICT, _stale_operator_context_payload(exc)
    return HTTPStatus.OK, document.to_json_payload()


def _load_execution_config(
    *,
    config_path: Path | None,
    workspace_root: Path,
) -> ExecutionConfig | None:
    if config_path is None:
        return None
    try:
        return load_execution_config_for_workspace(
            config_path,
            workspace_root=workspace_root,
        )
    except BootstrapConfigError as exc:
        raise PMConsoleServerError(str(exc)) from exc


def _resolve_config_path(
    *,
    config_path: Path | None,
    workspace_root: Path,
    runtime_mode: str,
) -> Path | None:
    """Resolve an explicit or repo-local kernel config for this workspace."""

    if config_path is not None:
        return config_path.expanduser().resolve(strict=False)

    config_dir = _find_repo_config_dir(workspace_root)
    if config_dir is None:
        return None
    mode = _resolve_workspace_config_mode(workspace_root, runtime_mode)
    if mode is None:
        return None
    target_hint = _workspace_target_hint(workspace_root, mode)
    candidates = sorted(config_dir.glob(f"kernel.{mode}.*.toml"))
    if runtime_mode == "catchup":
        catchup_candidates = [path for path in candidates if "catchup" in path.stem]
        if catchup_candidates:
            candidates = catchup_candidates
    if target_hint is not None:
        preferred = config_dir / f"kernel.{mode}.{target_hint}.toml"
        if preferred in candidates:
            candidates = [preferred, *[path for path in candidates if path != preferred]]

    matching = [path for path in candidates if _config_matches_workspace(path, workspace_root)]
    if target_hint is not None:
        preferred = config_dir / f"kernel.{mode}.{target_hint}.toml"
        if preferred in matching:
            return preferred
    if len(matching) == 1:
        return matching[0]
    if target_hint is not None:
        target_matches = [
            path
            for path in matching
            if path.stem.split(".", 2)[-1].split(".", 1)[0].split("-", 1)[0] == target_hint
        ]
        if len(target_matches) == 1:
            return target_matches[0]
    return None


def _find_repo_config_dir(workspace_root: Path) -> Path | None:
    roots = (Path.cwd().resolve(strict=False), *workspace_root.parents)
    for root in roots:
        config_dir = root / "config"
        if config_dir.is_dir():
            return config_dir
    return None


def _resolve_workspace_config_mode(workspace_root: Path, runtime_mode: str) -> str | None:
    if runtime_mode in {"live", "replay"}:
        return runtime_mode
    if runtime_mode == "catchup":
        return "live"
    workspace_name = workspace_root.name
    if workspace_name.startswith("live-"):
        return "live"
    if workspace_name.startswith("replay-"):
        return "replay"
    return None


def _workspace_target_hint(workspace_root: Path, mode: str) -> str | None:
    workspace_name = workspace_root.name.removesuffix("-workspace")
    prefix = f"{mode}-"
    if not workspace_name.startswith(prefix):
        return None
    remainder = workspace_name[len(prefix) :]
    return remainder.split("-", 1)[0] or None


def _config_matches_workspace(config_path: Path, workspace_root: Path) -> bool:
    try:
        config_workspace_root, _ = load_workspace_execution_config(config_path)
    except BootstrapConfigError:
        return False
    return config_workspace_root == workspace_root.resolve(strict=False)


def _resolve_reader_config(*, config_path: Path | None) -> CheckerAgentConfig | None:
    if config_path is None:
        return None
    try:
        config = load_kernel_config(config_path)
    except BootstrapConfigError:
        return None
    checker_config = config.checker_agent
    if checker_config is None or checker_config.llm_provider != "openai":
        return None
    return checker_config


def _build_thesis_translation_service(
    *,
    layout: WorkspaceLayout,
    reader_config: CheckerAgentConfig | None,
) -> PMConsoleThesisTranslationService:
    translator = _build_reader_translator(reader_config=reader_config)
    return PMConsoleThesisTranslationService(layout, translator=translator)


def _build_thesis_quick_brief_service(
    *,
    layout: WorkspaceLayout,
    reader_config: CheckerAgentConfig | None,
) -> PMConsoleThesisQuickBriefService:
    generator = None
    if reader_config is not None:
        generator = OpenAICompatibleThesisQuickBriefGenerator(
            api_key=reader_config.llm_api_key,
            model=reader_config.llm_model_name,
            base_url=reader_config.llm_base_url,
        )
    return PMConsoleThesisQuickBriefService(layout, generator=generator)


def _build_reader_translator(
    *,
    reader_config: CheckerAgentConfig | None,
) -> OpenAICompatibleThesisSectionTranslator | None:
    if reader_config is None:
        return None
    return OpenAICompatibleThesisSectionTranslator(
        api_key=reader_config.llm_api_key,
        model=reader_config.llm_model_name,
        base_url=reader_config.llm_base_url,
    )


def _target_from_position_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "position"
    ):
        return segments[2]
    return None


def _target_from_episodes_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "episodes"
    ):
        return segments[2]
    return None


def _target_and_episode_detail_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 5
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "episodes"
    ):
        return segments[2], segments[4]
    return None


def _query_value(raw_query: str, key: str) -> str | None:
    values = parse_qs(raw_query, keep_blank_values=False).get(key, ())
    return values[0] if values else None


def _query_int(raw_query: str, key: str, default: int) -> int:
    value = _query_value(raw_query, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise PMConsoleServerError(f"{key} must be an integer") from exc


def _target_from_thesis_revisions_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "thesis-revisions"
    ):
        return segments[2]
    return None


def _target_from_context_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "context"
    ):
        return segments[2]
    return None


def _target_from_operator_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "operator"
    ):
        return segments[2]
    return None


def _target_from_operator_history_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 5
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3:5] == ["operator", "history"]
    ):
        return segments[2]
    return None


def _target_and_operator_history_version_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 6
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3:5] == ["operator", "history"]
    ):
        return segments[2], segments[5]
    return None


def _target_and_operator_restore_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 7
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3:5] == ["operator", "history"]
        and segments[6] == "restore"
    ):
        return segments[2], segments[5]
    return None


def _target_from_pm_decisions_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "pm-decisions"
    ):
        return segments[2]
    return None


def _target_and_decision_from_pm_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 5
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "pm-decisions"
    ):
        return segments[2], segments[4]
    return None


def _target_and_assessment_from_analysis_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 5
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "analysis-assessments"
    ):
        return segments[2], segments[4]
    return None


def _target_and_revision_from_thesis_detail_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 5
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "thesis-revisions"
    ):
        return segments[2], segments[4]
    return None


def _target_and_revision_from_translate_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 6
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "thesis-revisions"
        and segments[5] == "translate"
    ):
        return segments[2], segments[4]
    return None


def _target_and_revision_from_brief_path(path: str) -> tuple[str, str] | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 6
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "thesis-revisions"
        and segments[5] == "brief"
    ):
        return segments[2], segments[4]
    return None


def _operator_target_layout(
    *,
    target: str,
    bindings: Mapping[str, PMConsoleBinding] | None,
    layout: WorkspaceLayout,
    runtime_mode: str,
    market_data_snapshot_id: str | None,
    display_start_at: datetime | None,
    execution_config: ExecutionConfig | None,
    translation_service: PMConsoleThesisTranslationService | None,
    thesis_quick_brief_service: PMConsoleThesisQuickBriefService | None,
) -> WorkspaceLayout | None:
    scope = _resolve_target_scope(
        target=target,
        bindings=bindings,
        layout=layout,
        runtime_mode=runtime_mode,
        market_data_snapshot_id=market_data_snapshot_id,
        display_start_at=display_start_at,
        execution_config=execution_config,
        translation_service=translation_service,
        thesis_quick_brief_service=thesis_quick_brief_service,
        known_targets=set(list_pm_console_targets(layout)),
    )
    return None if scope is None else scope[0]


def _stale_operator_context_payload(
    exc: OperatorContextConflictError,
) -> dict[str, object]:
    current = exc.current.to_json_payload()
    return {
        "error": "stale_operator_context",
        "error_message": str(exc),
        "current": current,
        "current_content_md": current["content_md"],
        "current_content_sha256": current["content_sha256"],
    }


def _parse_operator_put_request_payload(raw_body: bytes) -> dict[str, str]:
    payload = _parse_json_object(raw_body, "operator context request")
    expected_fields = {"content_md", "expected_content_sha256"}
    _require_exact_fields(payload, expected_fields, "operator context request")
    content_md = payload.get("content_md")
    expected_hash = payload.get("expected_content_sha256")
    if not isinstance(content_md, str) or not isinstance(expected_hash, str):
        raise PMConsoleServerError(
            "operator context request content_md and expected_content_sha256 must be strings."
        )
    return {"content_md": content_md, "expected_content_sha256": expected_hash}


def _parse_operator_restore_request_payload(raw_body: bytes) -> dict[str, str]:
    payload = _parse_json_object(raw_body, "operator restore request")
    _require_exact_fields(payload, {"expected_content_sha256"}, "operator restore request")
    expected_hash = payload.get("expected_content_sha256")
    if not isinstance(expected_hash, str):
        raise PMConsoleServerError("operator restore expected_content_sha256 must be a string.")
    return {"expected_content_sha256": expected_hash}


def _parse_json_object(raw_body: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PMConsoleServerError(f"{label} must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PMConsoleServerError(f"{label} must be a JSON object.")
    return payload


def _require_exact_fields(
    payload: Mapping[str, object],
    expected_fields: set[str],
    label: str,
) -> None:
    actual_fields = {str(key) for key in payload}
    missing_fields = sorted(expected_fields - actual_fields)
    unexpected_fields = sorted(actual_fields - expected_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing fields: {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected fields: {', '.join(unexpected_fields)}")
        raise PMConsoleServerError(f"{label} " + "; ".join(problems))


def _parse_translation_request_payload(raw_body: bytes) -> PMConsoleThesisTranslationRequestDTO:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PMConsoleServerError("request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PMConsoleServerError("request body must be a JSON object.")
    expected_fields = {"canonical_bundle_sha256", "locale"}
    actual_fields = set(str(key) for key in payload)
    missing_fields = sorted(expected_fields - actual_fields)
    unexpected_fields = sorted(actual_fields - expected_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing fields: {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected fields: {', '.join(unexpected_fields)}")
        raise PMConsoleServerError("translation request " + "; ".join(problems))
    canonical_bundle_sha256 = payload.get("canonical_bundle_sha256")
    locale = payload.get("locale")
    if not isinstance(canonical_bundle_sha256, str) or not isinstance(locale, str):
        raise PMConsoleServerError(
            "translation request canonical_bundle_sha256 and locale must be strings."
        )
    try:
        return PMConsoleThesisTranslationRequestDTO(
            canonical_bundle_sha256=canonical_bundle_sha256,
            locale=locale,
        )
    except PMConsoleSchemaError as exc:
        raise PMConsoleServerError(str(exc)) from exc


def _parse_quick_brief_request_payload(raw_body: bytes) -> PMConsoleThesisQuickBriefRequestDTO:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PMConsoleServerError("request body must be valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"canonical_bundle_sha256"}:
        raise PMConsoleServerError("quick brief request must contain only canonical_bundle_sha256.")
    canonical_bundle_sha256 = payload.get("canonical_bundle_sha256")
    if not isinstance(canonical_bundle_sha256, str):
        raise PMConsoleServerError("quick brief canonical_bundle_sha256 must be a string.")
    try:
        return PMConsoleThesisQuickBriefRequestDTO(
            canonical_bundle_sha256=canonical_bundle_sha256,
        )
    except PMConsoleSchemaError as exc:
        raise PMConsoleServerError(str(exc)) from exc


def _position_market_data_kwargs(
    *,
    bindings: Mapping[str, PMConsoleBinding] | None,
    target: str,
    layout: WorkspaceLayout,
    market_data_snapshot_id: str | None,
) -> _PositionMarketDataKwargs:
    binding = None if bindings is None else bindings.get(target)
    return {
        "market_data_root": (
            binding.market_data_root
            if binding is not None and binding.market_data_root is not None
            else shared_market_data_root()
        ),
        "market_data_snapshot_id": (
            binding.market_data_snapshot_id
            if binding is not None and binding.market_data_snapshot_id is not None
            else read_workspace_snapshot_ref(
                layout.root,
                target_key=target,
                run_id=None,
            )
        ),
        "market_data_provider_name": (
            None if binding is None else binding.market_data_provider_name
        ),
        "market_mapping": None if binding is None else binding.market_data_mapping,
    }


def _build_position_response_for_target(
    *,
    layout: WorkspaceLayout,
    target: str,
    runtime_mode: str,
    display_start_at: datetime | None,
    execution_config: ExecutionConfig | None,
    market_data_root: Path | None = None,
    market_data_snapshot_id: str | None = None,
    market_data_provider_name: str | None = None,
    market_mapping: MarketMapping | None = None,
):
    buy_hold_entry_cost_bps = None
    if execution_config is not None:
        buy_hold_entry_cost_bps = resolve_execution_buy_cost_bps_for_target(
            execution_config,
            target_key=target,
        )
    snapshot = build_position_monitoring_snapshot(
        layout=layout,
        target_key=target,
        buy_hold_entry_cost_bps=buy_hold_entry_cost_bps,
        runtime_mode=runtime_mode,
        display_start_at=display_start_at,
        market_data_root=market_data_root,
        market_data_snapshot_id=market_data_snapshot_id,
        market_data_provider_name=market_data_provider_name,
        market_mapping=market_mapping,
    )
    return build_position_response(snapshot)


def _write_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local PM Console API server.")
    parser.add_argument(
        "--buy-hold-results-dir", type=Path, default=None,
        help="Optional local Buy & Hold overlays for --saved-results sample views.",
    )
    parser.add_argument(
        "--saved-results",
        action="store_true",
        help=(
            "Read saved position curves without raw market bars; "
            "disable writes and model calls."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Workspace root to read, for example .local/live-sox-workspace.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Optional kernel config file. Without it, the matching repo-local live/replay "
            "kernel config is resolved from the workspace."
        ),
    )
    parser.add_argument(
        "--mount-catalog",
        type=Path,
        default=None,
        help=(
            "Persistent TOML catalog containing config-only [[mount]] entries for "
            "multi-target mode."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Defaults to 8765.")
    parser.add_argument(
        "--runtime-mode",
        choices=("live", "replay", "catchup", "unknown"),
        default="unknown",
        help="Optional local runtime mode label for the PM Console context endpoint.",
    )
    parser.add_argument(
        "--market-data-snapshot-id",
        default=None,
        help=(
            "Optional pinned shared market-data snapshot id. "
            "Use this when replay workspaces contain multiple market-data runs."
        ),
    )
    parser.add_argument(
        "--display-start-at",
        type=_parse_display_start_at,
        default=None,
        help=(
            "Optional UTC display and PnL baseline timestamp. The PM position immediately "
            "before this time is carried into the first displayed market bar."
        ),
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Allow binding the local PM Console API server to a non-loopback host.",
    )
    return parser.parse_args(argv)


def _parse_display_start_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("display-start-at must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("display-start-at must include a timezone offset.")
    return parsed.astimezone(UTC)


__all__ = [
    "PMConsoleServerError",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
