"""Local read-only PM Console API server."""

from __future__ import annotations

import argparse
import json
import os
import traceback
import re
import tomllib
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from event_trader.config import (
    _load_dotenv_from_cwd,
    BootstrapConfigError,
    ExecutionConfig,
    load_execution_config_for_workspace,
    resolve_execution_buy_cost_bps_for_target,
    resolve_execution_costs_for_target,
    resolve_config_relative_path,
)
from event_trader.execution import ExecutionCostModel
from event_trader.pm_console import (
    OpenAICompatibleThesisSectionTranslator,
    PMConsoleSchemaError,
    PMConsoleThesisTranslationFailure,
    PMConsoleThesisTranslationRequestDTO,
    PMConsoleThesisTranslationService,
    build_context_response,
    build_position_response,
    build_targets_response,
    build_thesis_revision_detail_response,
    build_thesis_revisions_response,
    build_workbench_response,
)
from event_trader.pm_console.thesis import list_pm_console_targets
from event_trader.position_monitoring import (
    build_position_monitoring_snapshot,
    list_position_monitoring_targets,
)
from event_trader.storage import WorkspaceLayout, build_workspace_layout


class PMConsoleServerError(ValueError):
    """Raised when the PM Console server cannot start or serve a request."""

_WORKSPACE_CONFIG_RE = re.compile(r"^(live|replay)-(?P<target>[a-z0-9_]+)-workspace$")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    host = args.host.strip()
    if host not in {"127.0.0.1", "localhost"} and not args.allow_non_loopback:
        raise SystemExit(
            "error: non-loopback hosts require --allow-non-loopback for the local "
            "PM Console server."
        )
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    if not workspace_root.exists() or not workspace_root.is_dir():
        raise SystemExit(f"error: workspace root does not exist: {workspace_root}")
    layout = build_workspace_layout(workspace_root)
    execution_config = _load_execution_config(
        config_path=args.config,
        workspace_root=layout.root,
    )
    thesis_translation_service = _build_thesis_translation_service(
        layout=layout,
        config_path=args.config,
    )
    server = ThreadingHTTPServer(
        (host, args.port),
        _build_handler(
            layout,
            runtime_mode=args.runtime_mode,
            market_data_run_id=args.market_data_run_id,
            execution_config=execution_config,
            thesis_translation_service=thesis_translation_service,
        ),
    )
    try:
        print(
            f"PM Console server listening on http://{host}:{server.server_port} "
            f"workspace_root={layout.root}"
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
    market_data_run_id: str | None,
    execution_config: ExecutionConfig | None = None,
    thesis_translation_service: PMConsoleThesisTranslationService | None = None,
) -> type[BaseHTTPRequestHandler]:
    translation_service = thesis_translation_service or _build_thesis_translation_service(
        layout=layout,
        config_path=None,
    )

    class PMConsoleHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            _write_cors_headers(self)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            try:
                status, payload = _dispatch_get(
                    layout=layout,
                    raw_path=self.path,
                    runtime_mode=runtime_mode,
                    market_data_run_id=market_data_run_id,
                    execution_config=execution_config,
                    translation_service=translation_service,
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
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                status, payload = _dispatch_post(
                    layout=layout,
                    raw_path=self.path,
                    raw_body=raw_body,
                    translation_service=translation_service,
                )
            except PMConsoleThesisTranslationFailure as exc:
                status = exc.status
                payload = exc.response.to_json_payload()
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


def _dispatch_get(
    *,
    layout: WorkspaceLayout,
    raw_path: str,
    runtime_mode: str = "unknown",
    market_data_run_id: str | None = None,
    execution_config: ExecutionConfig | None = None,
    translation_service: PMConsoleThesisTranslationService | None = None,
) -> tuple[HTTPStatus, dict[str, object]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    if path == "/api/context":
        return (
            HTTPStatus.OK,
            build_context_response(
                layout,
                runtime_mode=runtime_mode,
                thesis_translation_service=translation_service,
            ).to_json_payload(),
        )
    if path == "/api/targets":
        return HTTPStatus.OK, build_targets_response(layout).to_json_payload()
    thesis_target = _target_from_thesis_revisions_path(path)
    if thesis_target is not None:
        known_targets = set(list_pm_console_targets(layout))
        if thesis_target not in known_targets:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {thesis_target}"}
        return (
            HTTPStatus.OK,
            build_thesis_revisions_response(
                layout,
                target_key=thesis_target,
            ).to_json_payload(),
        )
    thesis_detail = _target_and_revision_from_thesis_detail_path(path)
    if thesis_detail is not None:
        target, revision_id = thesis_detail
        known_targets = set(list_pm_console_targets(layout))
        if target not in known_targets:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        payload = build_thesis_revision_detail_response(
            layout,
            target_key=target,
            revision_id=revision_id,
        )
        if payload is None:
            return (
                HTTPStatus.NOT_FOUND,
                {
                    "error": (
                        "unknown thesis revision: "
                        f"target={target} revision_id={revision_id}"
                    )
                },
            )
        return HTTPStatus.OK, payload.to_json_payload()
    target = _target_from_workbench_path(path)
    if target is not None:
        known_targets = set(list_pm_console_targets(layout))
        if target not in known_targets:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        position_response = None
        if target in set(list_position_monitoring_targets(layout)):
            position_response = _build_position_response_for_target(
                layout=layout,
                target=target,
                runtime_mode=runtime_mode,
                market_data_run_id=market_data_run_id,
                execution_config=execution_config,
            )
        return (
            HTTPStatus.OK,
            build_workbench_response(
                layout,
                target_key=target,
                position_response=position_response,
            ).to_json_payload(),
        )
    target = _target_from_position_path(path)
    if target is not None:
        known_targets = set(list_position_monitoring_targets(layout))
        if target not in known_targets:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
        return (
            HTTPStatus.OK,
            _build_position_response_for_target(
                layout=layout,
                target=target,
                runtime_mode=runtime_mode,
                market_data_run_id=market_data_run_id,
                execution_config=execution_config,
            ).to_json_payload(),
        )
    return HTTPStatus.NOT_FOUND, {"error": f"unknown route: {path}"}


def _dispatch_post(
    *,
    layout: WorkspaceLayout,
    raw_path: str,
    raw_body: bytes,
    translation_service: PMConsoleThesisTranslationService,
) -> tuple[HTTPStatus, dict[str, object]]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    translation_detail = _target_and_revision_from_translate_path(path)
    if translation_detail is None:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown route: {path}"}
    target, revision_id = translation_detail
    known_targets = set(list_pm_console_targets(layout))
    if target not in known_targets:
        return HTTPStatus.NOT_FOUND, {"error": f"unknown target: {target}"}
    request = _parse_translation_request_payload(raw_body)
    payload = translation_service.translate_revision(
        target_key=target,
        revision_id=revision_id,
        request=request,
    )
    return HTTPStatus.OK, payload.to_json_payload()


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


def _build_thesis_translation_service(
    *,
    layout: WorkspaceLayout,
    config_path: Path | None,
) -> PMConsoleThesisTranslationService:
    translator = _resolve_thesis_translation_translator(
        layout=layout,
        config_path=config_path,
    )
    return PMConsoleThesisTranslationService(layout, translator=translator)


def _resolve_thesis_translation_translator(
    *,
    layout: WorkspaceLayout,
    config_path: Path | None,
) -> OpenAICompatibleThesisSectionTranslator | None:
    config_translator = _load_translation_translator_from_config(
        layout=layout,
        config_path=config_path,
    )
    if config_translator is not None:
        return config_translator
    return OpenAICompatibleThesisSectionTranslator.from_environment()


def _load_translation_translator_from_config(
    *,
    layout: WorkspaceLayout,
    config_path: Path | None,
) -> OpenAICompatibleThesisSectionTranslator | None:
    resolved_config_path = _resolve_pm_console_config_path(
        config_path=config_path,
        workspace_root=layout.root,
    )
    if resolved_config_path is None:
        return None
    try:
        raw_config = _read_pm_console_config_table(resolved_config_path)
        config_workspace_root = _resolve_config_workspace_root(
            raw_config=raw_config,
            config_path=resolved_config_path,
        )
        if config_workspace_root != layout.root:
            raise PMConsoleServerError(
                "Config workspace_root does not match the requested PM Console workspace: "
                f"config={config_workspace_root} requested={layout.root}"
            )
        checker_agent = _require_config_table(
            raw_config.get("checker_agent"),
            field_name="checker_agent",
        )
        llm_provider = _require_non_blank_config_string(
            checker_agent.get("llm_provider"),
            field_name="checker_agent.llm_provider",
        )
        llm_model_name = _require_non_blank_config_string(
            checker_agent.get("llm_model_name"),
            field_name="checker_agent.llm_model_name",
        )
        llm_api_key_env = _require_non_blank_config_string(
            checker_agent.get("llm_api_key_env"),
            field_name="checker_agent.llm_api_key_env",
        )
        llm_base_url = _require_non_blank_config_string(
            checker_agent.get("llm_base_url"),
            field_name="checker_agent.llm_base_url",
        )
    except BootstrapConfigError as exc:
        raise PMConsoleServerError(str(exc)) from exc
    if llm_provider != "openai":
        return None
    _load_dotenv_from_cwd()
    api_key = os.getenv(llm_api_key_env, "").strip()
    if not api_key:
        return None
    return OpenAICompatibleThesisSectionTranslator(
        api_key=api_key,
        model=llm_model_name,
        base_url=llm_base_url,
    )


def _resolve_pm_console_config_path(
    *,
    config_path: Path | None,
    workspace_root: Path,
) -> Path | None:
    if config_path is not None:
        return config_path.expanduser().resolve(strict=False)
    workspace_match = _WORKSPACE_CONFIG_RE.match(workspace_root.name)
    if workspace_match is None:
        return None
    mode = workspace_match.group(1)
    target_key = workspace_match.group("target")
    workspace_parent = workspace_root.parent
    if workspace_parent.name != ".local":
        return None
    repo_root = workspace_parent.parent
    candidate = (repo_root / "config" / f"kernel.{mode}.{target_key}.toml").resolve(
        strict=False
    )
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _read_pm_console_config_table(config_path: Path) -> dict[str, object]:
    try:
        with config_path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise BootstrapConfigError(f"Config file does not exist: {config_path}") from exc
    except PermissionError as exc:
        raise BootstrapConfigError(f"Config file is not readable: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BootstrapConfigError(
            f"Config file contains invalid TOML: {config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise BootstrapConfigError(f"Could not read config file {config_path}: {exc}") from exc
    if not isinstance(raw_config, dict):
        raise BootstrapConfigError("Config file must decode to a TOML table.")
    return raw_config


def _resolve_config_workspace_root(
    *,
    raw_config: dict[str, object],
    config_path: Path,
) -> Path:
    raw_workspace_root = raw_config.get("workspace_root")
    if not isinstance(raw_workspace_root, str) or not raw_workspace_root.strip():
        raise BootstrapConfigError("Missing required config field: workspace_root")
    workspace_root = Path(raw_workspace_root.strip()).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = resolve_config_relative_path(workspace_root, config_path=config_path)
    return workspace_root.resolve(strict=False)


def _require_config_table(value: object, *, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BootstrapConfigError(f"{field_name} must be a TOML table.")
    return value


def _require_non_blank_config_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapConfigError(f"{field_name} must be a non-blank string.")
    return value.strip()


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


def _target_from_workbench_path(path: str) -> str | None:
    segments = [unquote(segment) for segment in path.split("/") if segment]
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "targets"
        and segments[3] == "workbench"
    ):
        return segments[2]
    return None


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
    try:
        return PMConsoleThesisTranslationRequestDTO(
            canonical_bundle_sha256=payload.get("canonical_bundle_sha256"),
            locale=payload.get("locale"),
        )
    except PMConsoleSchemaError as exc:
        raise PMConsoleServerError(str(exc)) from exc


def _build_position_response_for_target(
    *,
    layout: WorkspaceLayout,
    target: str,
    runtime_mode: str,
    market_data_run_id: str | None,
    execution_config: ExecutionConfig | None,
):
    buy_hold_entry_cost_bps = None
    shadow_cost_model = None
    if execution_config is not None:
        buy_hold_entry_cost_bps = resolve_execution_buy_cost_bps_for_target(
            execution_config,
            target_key=target,
        )
        buy_cost_bps, sell_cost_bps = resolve_execution_costs_for_target(
            execution_config,
            target_key=target,
        )
        shadow_cost_model = ExecutionCostModel(
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            price_basis=execution_config.price_basis,
        )
    snapshot = build_position_monitoring_snapshot(
        layout=layout,
        target_key=target,
        buy_hold_entry_cost_bps=buy_hold_entry_cost_bps,
        shadow_cost_model=shadow_cost_model,
        runtime_mode=runtime_mode,
        market_data_run_id=market_data_run_id,
    )
    return build_position_response(snapshot)


def _write_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local PM Console API server.")
    parser.add_argument(
        "--workspace-root",
        required=True,
        help="Workspace root to read, for example .local/live-sox-workspace.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Optional kernel config file. Used for baseline execution costs and, when "
            "checker_agent is OpenAI-compatible, Thesis Evolution translation. "
            "When omitted, live/replay workspaces like .local/live-sox-workspace "
            "auto-resolve config/kernel.live.sox.toml when present."
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
        "--market-data-run-id",
        default=None,
        help=(
            "Optional replay market-data run id under runtime/market_data/<target>/<run-id>. "
            "Use this when replay workspaces contain multiple market-data runs."
        ),
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Allow binding the local PM Console API server to a non-loopback host.",
    )
    return parser.parse_args(argv)


__all__ = [
    "PMConsoleServerError",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
