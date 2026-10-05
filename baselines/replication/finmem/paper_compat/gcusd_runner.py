from __future__ import annotations

import copy
import json
import os
import pickle
import sys
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import toml

SCRIPT_PATH = Path(__file__).resolve()
WORKSPACE_ROOT = SCRIPT_PATH.parents[3]
FINMEM_ROOT = WORKSPACE_ROOT / "FinMem-LLM-StockTrading"
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))
if str(FINMEM_ROOT) not in sys.path:
    sys.path.insert(0, str(FINMEM_ROOT))

from puppy import LLMAgent, MarketEnvironment, RunMode  # noqa: E402

from build_gcusd_env import (  # noqa: E402
    DEFAULT_DAILY_OHLCV,
    DEFAULT_NEWS_ARCHIVE,
    build_env_data,
)


DEFAULT_CONFIG_PATH = SCRIPT_PATH.parent / "gcusd_m2_1_paper_compat.toml"
USAGE_FIELDS = (
    "llm_calls",
    "tokens_in",
    "tokens_out",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default))
        handle.write("\n")


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in USAGE_FIELDS}


def _usage_diff(after: dict[str, Any], before: dict[str, Any]) -> dict[str, int]:
    return {
        field: int(after.get(field, 0) or 0) - int(before.get(field, 0) or 0)
        for field in USAGE_FIELDS
    }


def _usage_add(left: dict[str, Any], right: dict[str, Any]) -> dict[str, int]:
    return {
        field: int(left.get(field, 0) or 0) + int(right.get(field, 0) or 0)
        for field in USAGE_FIELDS
    }


def _total_tokens(usage: dict[str, Any]) -> int:
    return int(usage.get("tokens_in", 0) or 0) + int(usage.get("tokens_out", 0) or 0)


def _usage_source(usage: dict[str, Any]) -> str:
    if int(usage.get("llm_calls", 0) or 0) <= 0:
        return "unavailable"
    token_fields = (
        "tokens_in",
        "tokens_out",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    if any(int(usage.get(field, 0) or 0) > 0 for field in token_fields):
        return "provider_reported"
    return "unavailable"


def _decorate_usage(usage: dict[str, Any]) -> dict[str, Any]:
    enriched = {field: int(usage.get(field, 0) or 0) for field in USAGE_FIELDS}
    enriched["total_tokens"] = _total_tokens(enriched)
    enriched["usage_source"] = _usage_source(enriched)
    return enriched


def _estimate_cost(
    usage: dict[str, Any],
    input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
) -> float | None:
    if input_price_per_mtok is None or output_price_per_mtok is None:
        return None
    return (
        usage.get("tokens_in", 0) / 1_000_000 * input_price_per_mtok
        + usage.get("tokens_out", 0) / 1_000_000 * output_price_per_mtok
    )


def _decision_cutoff_utc(trade_day: date) -> datetime:
    return datetime.combine(
        trade_day,
        datetime.max.time().replace(microsecond=0),
        tzinfo=timezone.utc,
    )


def _finmem_step_direction(decision: str | None) -> int:
    mapping = {"buy": 1, "hold": 0, "sell": -1}
    return mapping.get((decision or "").lower(), 0)


def _position_sign(position_units: int) -> str:
    if position_units > 0:
        return "long"
    if position_units < 0:
        return "short"
    return "flat"


def _prepare_finmem_runtime() -> None:
    os.chdir(FINMEM_ROOT)
    (FINMEM_ROOT / "data" / "04_model_output_log").mkdir(parents=True, exist_ok=True)


def _load_config(config_path: Path) -> dict[str, Any]:
    return toml.load(config_path)


def _maybe_add_synthetic_terminal_row(
    *,
    env_data: OrderedDict[date, dict[str, Any]],
    receipt: dict[str, Any],
    test_end: date,
) -> tuple[OrderedDict[date, dict[str, Any]], dict[str, Any]]:
    terminal_day = test_end + timedelta(days=1)
    if terminal_day in env_data:
        receipt["synthetic_terminal_row"] = {
            "added": False,
            "date": terminal_day.isoformat(),
        }
        return env_data, receipt
    if test_end not in env_data:
        raise ValueError(f"Missing terminal trade day in env data: {test_end.isoformat()}")

    last_close = env_data[test_end]["price"]["GCUSD"]
    env_data_with_terminal = OrderedDict(env_data)
    env_data_with_terminal[terminal_day] = {
        "price": {"GCUSD": last_close},
        "filing_k": {},
        "filing_q": {},
        "news": {},
    }
    ordered = OrderedDict(sorted(env_data_with_terminal.items(), key=lambda item: item[0]))
    receipt = dict(receipt)
    receipt["synthetic_terminal_row"] = {
        "added": True,
        "date": terminal_day.isoformat(),
        "close": last_close,
    }
    return ordered, receipt


def _build_manifest(
    *,
    daily_ohlcv: Path,
    news_archive: Path,
    config_path: Path,
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
    max_news_per_day: int,
    input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "baseline": "finmem",
        "symbol": config["general"]["trading_symbol"],
        "daily_ohlcv": str(daily_ohlcv),
        "daily_ohlcv_size": daily_ohlcv.stat().st_size,
        "daily_ohlcv_mtime_ns": daily_ohlcv.stat().st_mtime_ns,
        "news_archive": str(news_archive),
        "config_path": str(config_path),
        "config_size": config_path.stat().st_size,
        "config_mtime_ns": config_path.stat().st_mtime_ns,
        "train_start": train_start.isoformat(),
        "train_end": train_end.isoformat(),
        "test_start": test_start.isoformat(),
        "test_end": test_end.isoformat(),
        "max_news_per_day": max_news_per_day,
        "pricing": {
            "input_price_per_mtok": input_price_per_mtok,
            "output_price_per_mtok": output_price_per_mtok,
        },
        "model": {
            "provider": "minimax",
            "chat_api_format": config["chat"].get("api_format"),
            "llm": config["chat"]["model"],
            "endpoint": config["chat"]["end_point"],
            "embedding_provider": config["agent"]["agent_1"]["embedding"]["detail"].get("provider", "openai"),
            "embedding_model": config["agent"]["agent_1"]["embedding"]["detail"]["embedding_model"],
        },
    }


def _ensure_manifest(output_dir: Path, desired_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        existing_manifest = _read_json(manifest_path)
        if existing_manifest != desired_manifest:
            raise ValueError(
                "Output directory manifest does not match the requested run inputs. "
                "Use a fresh output directory for a different FinMem GCUSD run."
            )
        return existing_manifest

    if any(output_dir.iterdir()):
        raise ValueError(
            f"Output directory is not empty and has no manifest: {output_dir}"
        )
    _write_json_atomic(manifest_path, desired_manifest)
    return desired_manifest


def _expected_warmup_days(env_data: OrderedDict[date, dict[str, Any]], train_start: date, train_end: date) -> list[str]:
    return [day.isoformat() for day in env_data if train_start <= day < train_end]


def _expected_test_days(env_data: OrderedDict[date, dict[str, Any]], test_start: date, test_end: date) -> list[str]:
    return [day.isoformat() for day in env_data if test_start <= day <= test_end]


def _default_state(
    *,
    warmup_days: list[str],
    test_days: list[str],
    synthetic_terminal_row: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": "warmup",
        "synthetic_terminal_row": synthetic_terminal_row,
        "warmup": {
            "expected_days": warmup_days,
            "last_committed_day": None,
            "completed_count": 0,
            "llm_usage": _zero_usage(),
            "elapsed_seconds": 0.0,
        },
        "test": {
            "expected_days": test_days,
            "last_committed_day": None,
            "completed_count": 0,
            "llm_usage": _zero_usage(),
            "elapsed_seconds": 0.0,
        },
    }


def _load_or_init_state(
    output_dir: Path,
    *,
    warmup_days: list[str],
    test_days: list[str],
    synthetic_terminal_row: dict[str, Any],
) -> dict[str, Any]:
    state_path = output_dir / "run_state.json"
    if state_path.exists():
        state = _read_json(state_path)
        if state["warmup"]["expected_days"] != warmup_days or state["test"]["expected_days"] != test_days:
            raise ValueError("Existing run_state does not match the current expected GCUSD calendar.")
        return state

    state = _default_state(
        warmup_days=warmup_days,
        test_days=test_days,
        synthetic_terminal_row=synthetic_terminal_row,
    )
    _write_json_atomic(state_path, state)
    return state


def _write_state(output_dir: Path, state: dict[str, Any]) -> None:
    _write_json_atomic(output_dir / "run_state.json", state)


def _committed_days(expected_days: list[str], last_committed_day: str | None) -> list[str]:
    if last_committed_day is None:
        return []
    if last_committed_day not in expected_days:
        raise ValueError(f"Committed day {last_committed_day} is outside the expected calendar.")
    last_index = expected_days.index(last_committed_day)
    return expected_days[: last_index + 1]


def _load_committed_day_audits(output_dir: Path, expected_days: list[str], last_committed_day: str | None) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for day in _committed_days(expected_days, last_committed_day):
        audit_path = output_dir / "days" / day / "audit.json"
        if not audit_path.exists():
            raise FileNotFoundError(f"Missing committed audit for {day}: {audit_path}")
        audits.append(_read_json(audit_path))
    return audits


def _checkpoint_path(root: Path, committed_day: str | None) -> Path | None:
    if committed_day is None:
        return None
    return root / committed_day


def _load_agent_env_checkpoint(checkpoint_dir: Path) -> tuple[LLMAgent, MarketEnvironment]:
    agent = LLMAgent.load_checkpoint(str(checkpoint_dir / "agent_1"))
    env = MarketEnvironment.load_checkpoint(str(checkpoint_dir / "env"))
    return agent, env


def _save_agent_env_checkpoint(
    *,
    checkpoint_root: Path,
    committed_day: str,
    agent: LLMAgent,
    env: MarketEnvironment,
) -> Path:
    checkpoint_dir = checkpoint_root / committed_day
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    agent.save_checkpoint(path=str(checkpoint_dir), force=True)
    env.save_checkpoint(path=str(checkpoint_dir), force=True)
    return checkpoint_dir


def _build_summary(
    *,
    output_dir: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    test_audits = _load_committed_day_audits(
        output_dir,
        state["test"]["expected_days"],
        state["test"]["last_committed_day"],
    )
    return {
        "baseline": "finmem",
        "symbol": manifest["symbol"],
        "reported_window": {
            "start": manifest["test_start"],
            "end": manifest["test_end"],
        },
        "manifest_path": str(output_dir / "manifest.json"),
        "synthetic_terminal_row": state["synthetic_terminal_row"],
        "warmup": state["warmup"],
        "test": state["test"],
        "runs": test_audits,
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = quantile * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _build_cost_report(
    *,
    output_dir: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    audits = _load_committed_day_audits(
        output_dir,
        state["test"]["expected_days"],
        state["test"]["last_committed_day"],
    )
    elapsed_values = [
        float(audit["timing"]["day_step"]["elapsed_seconds"])
        for audit in audits
    ]
    day_usages = [audit["llm_usage"]["day_audit"] for audit in audits]
    provider_reported_count = sum(
        1 for usage in day_usages if usage.get("usage_source") == "provider_reported"
    )
    unavailable_count = sum(
        1 for usage in day_usages if usage.get("usage_source") == "unavailable"
    )
    warnings: list[str] = []
    if unavailable_count:
        warnings.append(
            f"{unavailable_count} decision(s) returned no provider token usage."
        )
    if state["phase"] != "complete":
        warnings.append("Run is not complete; report covers committed decisions only.")

    slowest = sorted(
        (
            {
                "trade_date": audit["trade_date"],
                "elapsed_seconds": audit["timing"]["day_step"]["elapsed_seconds"],
                "native_action": audit["decision"].get("native_action", audit["decision"].get("raw")),
                "position_after_decision": (
                    audit.get("portfolio", {}).get("position_after_decision")
                    or "unknown"
                ),
            }
            for audit in audits
        ),
        key=lambda item: item["elapsed_seconds"],
        reverse=True,
    )[:5]

    usage_totals = _decorate_usage(state["test"]["llm_usage"])
    estimated_total_cost = _estimate_cost(
        state["test"]["llm_usage"],
        manifest["pricing"]["input_price_per_mtok"],
        manifest["pricing"]["output_price_per_mtok"],
    )

    return {
        "run_id": output_dir.name,
        "baseline_id": "finmem",
        "market_symbol": manifest["symbol"],
        "window_start": manifest["test_start"],
        "window_end": manifest["test_end"],
        "phase": state["phase"],
        "decision_count": len(audits),
        "failed_decision_count": sum(
            1 for audit in audits if audit["decision"].get("raw") is None
        ),
        "wall_span_seconds": state["warmup"]["elapsed_seconds"] + state["test"]["elapsed_seconds"],
        "timing_summary": {
            "total_seconds": sum(elapsed_values),
            "average_seconds": mean(elapsed_values) if elapsed_values else 0.0,
            "p50_seconds": _percentile(elapsed_values, 0.50),
            "p90_seconds": _percentile(elapsed_values, 0.90),
            "max_seconds": max(elapsed_values) if elapsed_values else 0.0,
        },
        "llm_usage_summary": {
            "record_count": len(day_usages),
            "provider_reported_count": provider_reported_count,
            "unavailable_count": unavailable_count,
            **usage_totals,
        },
        "estimated_total_cost_usd": estimated_total_cost,
        "module_breakdown": {
            "warmup_elapsed_seconds": state["warmup"]["elapsed_seconds"],
            "test_elapsed_seconds": state["test"]["elapsed_seconds"],
            "decision_total_seconds": sum(elapsed_values),
        },
        "slowest_decisions": slowest,
        "warnings": warnings,
    }


def _write_cost_reports(
    *,
    output_dir: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
) -> tuple[Path, Path]:
    cost_report = _build_cost_report(
        output_dir=output_dir,
        manifest=manifest,
        state=state,
    )
    json_path = output_dir / "cost_report.json"
    md_path = output_dir / "cost_report.md"
    _write_json_atomic(json_path, cost_report)
    markdown = "\n".join(
        [
            "# FinMem GCUSD Cost Report",
            "",
            f"- run_id: `{cost_report['run_id']}`",
            f"- window: `{cost_report['window_start']}` through `{cost_report['window_end']}`",
            f"- phase: `{cost_report['phase']}`",
            f"- decision_count: `{cost_report['decision_count']}`",
            f"- wall_span_seconds: `{cost_report['wall_span_seconds']:.3f}`",
            f"- average_decision_seconds: `{cost_report['timing_summary']['average_seconds']:.3f}`",
            f"- p90_decision_seconds: `{cost_report['timing_summary']['p90_seconds']:.3f}`",
            f"- llm_calls: `{cost_report['llm_usage_summary']['llm_calls']}`",
            f"- input_tokens: `{cost_report['llm_usage_summary']['tokens_in']}`",
            f"- output_tokens: `{cost_report['llm_usage_summary']['tokens_out']}`",
            f"- total_tokens: `{cost_report['llm_usage_summary']['total_tokens']}`",
            f"- provider_reported_count: `{cost_report['llm_usage_summary']['provider_reported_count']}`",
            f"- unavailable_count: `{cost_report['llm_usage_summary']['unavailable_count']}`",
            (
                f"- estimated_total_cost_usd: `{cost_report['estimated_total_cost_usd']:.6f}`"
                if cost_report["estimated_total_cost_usd"] is not None
                else "- estimated_total_cost_usd: `not_estimated`"
            ),
            (
                "- warnings: " + "; ".join(cost_report["warnings"])
                if cost_report["warnings"]
                else "- warnings: `none`"
            ),
        ]
    )
    md_path.write_text(markdown + "\n", encoding="utf-8")
    return json_path, md_path


def _write_summary(output_dir: Path, manifest: dict[str, Any], state: dict[str, Any]) -> Path:
    summary = _build_summary(output_dir=output_dir, manifest=manifest, state=state)
    summary_path = output_dir / "summary.json"
    _write_json_atomic(summary_path, summary)
    return summary_path


def _find_receipt_for_day(receipt: dict[str, Any], trade_day: str) -> dict[str, Any] | None:
    for item in receipt.get("days", []):
        if item.get("day") == trade_day:
            return item
    return None


def run_experiment(
    *,
    output_dir: Path,
    daily_ohlcv: Path = DEFAULT_DAILY_OHLCV,
    news_archive: Path = DEFAULT_NEWS_ARCHIVE,
    config_path: Path = DEFAULT_CONFIG_PATH,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    max_news_per_day: int,
    input_price_per_mtok: float | None = None,
    output_price_per_mtok: float | None = None,
) -> Path:
    _load_dotenv(FINMEM_ROOT / ".env")
    _load_dotenv(WORKSPACE_ROOT / ".env")
    if not os.environ.get("MINIMAX_API_KEY"):
        raise RuntimeError("Missing MINIMAX_API_KEY")

    train_start_date = datetime.strptime(train_start, "%Y-%m-%d").date()
    train_end_date = datetime.strptime(train_end, "%Y-%m-%d").date()
    test_start_date = datetime.strptime(test_start, "%Y-%m-%d").date()
    test_end_date = datetime.strptime(test_end, "%Y-%m-%d").date()

    resolved_output_dir = output_dir.resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    resolved_daily_ohlcv = daily_ohlcv.resolve()
    resolved_news_archive = news_archive.resolve()
    resolved_config_path = config_path.resolve()
    config = _load_config(resolved_config_path)

    manifest = _build_manifest(
        daily_ohlcv=resolved_daily_ohlcv,
        news_archive=resolved_news_archive,
        config_path=resolved_config_path,
        train_start=train_start_date,
        train_end=train_end_date,
        test_start=test_start_date,
        test_end=test_end_date,
        max_news_per_day=max_news_per_day,
        input_price_per_mtok=input_price_per_mtok,
        output_price_per_mtok=output_price_per_mtok,
        config=config,
    )
    _ensure_manifest(resolved_output_dir, manifest)

    _prepare_finmem_runtime()

    env_end = test_end_date + timedelta(days=1)
    env_data, env_receipt = build_env_data(
        daily_ohlcv=resolved_daily_ohlcv,
        news_archive=resolved_news_archive,
        start_date=train_start_date,
        end_date=env_end,
        max_news_per_day=max_news_per_day,
    )
    env_data, env_receipt = _maybe_add_synthetic_terminal_row(
        env_data=env_data,
        receipt=env_receipt,
        test_end=test_end_date,
    )

    inputs_dir = resolved_output_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    env_pickle_path = inputs_dir / f"env_data_gcusd_{train_start_date:%Y%m%d}_{env_end:%Y%m%d}.pkl"
    env_receipt_path = env_pickle_path.with_suffix(".receipt.json")
    with env_pickle_path.open("wb") as handle:
        pickle.dump(env_data, handle)
    _write_json_atomic(env_receipt_path, env_receipt)

    warmup_days = _expected_warmup_days(env_data, train_start_date, train_end_date)
    test_days = _expected_test_days(env_data, test_start_date, test_end_date)
    state = _load_or_init_state(
        resolved_output_dir,
        warmup_days=warmup_days,
        test_days=test_days,
        synthetic_terminal_row=env_receipt["synthetic_terminal_row"],
    )

    warmup_checkpoint_root = resolved_output_dir / "checkpoints" / "warmup"
    test_checkpoint_root = resolved_output_dir / "checkpoints" / "test"

    if state["phase"] == "warmup" and state["warmup"]["completed_count"] == len(warmup_days):
        state = copy.deepcopy(state)
        state["phase"] = "test"
        _write_state(resolved_output_dir, state)

    if state["phase"] == "warmup":
        if state["warmup"]["last_committed_day"] is None:
            warmup_env = MarketEnvironment(
                symbol=config["general"]["trading_symbol"],
                env_data_pkl=env_data,
                start_date=train_start_date,
                end_date=train_end_date,
            )
            warmup_agent = LLMAgent.from_config(config)
        else:
            checkpoint_dir = _checkpoint_path(warmup_checkpoint_root, state["warmup"]["last_committed_day"])
            if checkpoint_dir is None:
                raise RuntimeError("Warmup checkpoint path missing")
            warmup_agent, warmup_env = _load_agent_env_checkpoint(checkpoint_dir)

        while True:
            market_info = warmup_env.step(expose_future_record=True)
            if market_info[-1]:
                break
            trade_day = market_info[0]
            trade_date = trade_day.isoformat()
            day_started = datetime.now(timezone.utc)
            wall_start = time.perf_counter()
            before_usage = warmup_agent.get_llm_usage_summary()
            warmup_agent.counter += 1
            warmup_agent.step(market_info=market_info, run_mode=RunMode.Train)  # type: ignore[arg-type]
            after_usage = warmup_agent.get_llm_usage_summary()
            elapsed = time.perf_counter() - wall_start
            day_finished = datetime.now(timezone.utc)
            day_usage = _usage_diff(after_usage, before_usage)

            checkpoint_dir = _save_agent_env_checkpoint(
                checkpoint_root=warmup_checkpoint_root,
                committed_day=trade_date,
                agent=warmup_agent,
                env=warmup_env,
            )
            _append_jsonl(
                resolved_output_dir / "warmup_history.jsonl",
                {
                    "day": trade_date,
                    "checkpoint_dir": str(checkpoint_dir),
                    "timing": {
                        "started_at_utc": day_started.isoformat(),
                        "finished_at_utc": day_finished.isoformat(),
                        "elapsed_seconds": elapsed,
                    },
                    "llm_usage": day_usage,
                },
            )

            next_state = copy.deepcopy(state)
            next_state["warmup"]["last_committed_day"] = trade_date
            next_state["warmup"]["completed_count"] += 1
            next_state["warmup"]["llm_usage"] = _usage_add(next_state["warmup"]["llm_usage"], day_usage)
            next_state["warmup"]["elapsed_seconds"] += elapsed
            if next_state["warmup"]["completed_count"] == len(warmup_days):
                next_state["phase"] = "test"
            _write_state(resolved_output_dir, next_state)
            state = next_state
            print(
                "  "
                f"warmup {trade_date} elapsed={elapsed:.1f}s "
                f"llm_calls={day_usage['llm_calls']} "
                f"tokens_in={day_usage['tokens_in']} "
                f"tokens_out={day_usage['tokens_out']}"
            )

    if state["phase"] == "complete":
        summary_path = _write_summary(resolved_output_dir, manifest, state)
        _write_cost_reports(
            output_dir=resolved_output_dir,
            manifest=manifest,
            state=state,
        )
        print(f"FinMem GCUSD run already complete. Summary rebuilt at {summary_path}")
        return summary_path

    if state["warmup"]["last_committed_day"] is None and warmup_days:
        raise RuntimeError("Warmup must complete before test can start.")

    if state["test"]["last_committed_day"] is None:
        test_env = MarketEnvironment(
            symbol=config["general"]["trading_symbol"],
            env_data_pkl=env_data,
            start_date=test_start_date,
            end_date=test_end_date + timedelta(days=1),
        )
        warmup_checkpoint_dir = _checkpoint_path(warmup_checkpoint_root, state["warmup"]["last_committed_day"])
        if warmup_checkpoint_dir is None:
            raise RuntimeError("Missing warmup checkpoint for test start.")
        test_agent = LLMAgent.load_checkpoint(str(warmup_checkpoint_dir / "agent_1"))
    else:
        checkpoint_dir = _checkpoint_path(test_checkpoint_root, state["test"]["last_committed_day"])
        if checkpoint_dir is None:
            raise RuntimeError("Missing test checkpoint for resume.")
        test_agent, test_env = _load_agent_env_checkpoint(checkpoint_dir)

    while True:
        market_info = test_env.step(expose_future_record=False)
        if market_info[-1]:
            break
        trade_day = market_info[0]
        trade_date = trade_day.isoformat()
        day_dir = resolved_output_dir / "days" / trade_date
        day_dir.mkdir(parents=True, exist_ok=True)
        day_started = datetime.now(timezone.utc)
        wall_start = time.perf_counter()
        before_usage = test_agent.get_llm_usage_summary()
        test_agent.counter += 1
        test_agent.step(market_info=market_info, run_mode=RunMode.Test)  # type: ignore[arg-type]
        after_usage = test_agent.get_llm_usage_summary()
        elapsed = time.perf_counter() - wall_start
        day_finished = datetime.now(timezone.utc)
        day_usage = _usage_diff(after_usage, before_usage)
        cumulative_test_usage = _usage_add(state["test"]["llm_usage"], day_usage)
        cumulative_total_usage = _usage_add(state["warmup"]["llm_usage"], cumulative_test_usage)
        cumulative_test_elapsed = state["test"]["elapsed_seconds"] + elapsed
        day_usage_audit = _decorate_usage(day_usage)
        cumulative_test_usage_audit = _decorate_usage(cumulative_test_usage)
        cumulative_total_usage_audit = _decorate_usage(cumulative_total_usage)

        raw_reflection = test_agent.reflection_result_series_dict.get(trade_day, {})
        raw_reflection_path = day_dir / "raw_reflection.json"
        _write_json_atomic(raw_reflection_path, raw_reflection)

        decision_at = _decision_cutoff_utc(trade_day)
        trade_day_receipt = _find_receipt_for_day(env_receipt, trade_date)
        raw_decision = raw_reflection.get("investment_decision")
        action_direction = int(test_agent.portfolio.action_series.get(trade_day, 0))
        position_units_after_decision = int(test_agent.portfolio.holding_shares)
        position_units_before_decision = position_units_after_decision - action_direction
        audit = {
            "baseline": "finmem",
            "symbol": "GCUSD",
            "trade_date": trade_date,
            "decision_semantics": "daily_eod",
            "decision_at_utc": decision_at.isoformat(),
            "visible_through_utc": decision_at.isoformat(),
            "execution_semantics": "next_available_daily_bar_after_decision_at",
            "reported_window": {
                "start": test_start_date.isoformat(),
                "end": test_end_date.isoformat(),
            },
            "warmup_window": {
                "start": train_start_date.isoformat(),
                "end": train_end_date.isoformat(),
            },
            "model": manifest["model"],
            "inputs": {
                "daily_ohlcv": str(resolved_daily_ohlcv),
                "news_archive": str(resolved_news_archive),
                "news_cutoff_utc": decision_at.isoformat(),
                "env_data_pickle": str(env_pickle_path),
                "env_data_receipt": str(env_receipt_path),
                "synthetic_terminal_row_added": state["synthetic_terminal_row"]["added"],
                "synthetic_terminal_row_date": state["synthetic_terminal_row"]["date"],
                "requires_next_day_row_for_env_progression": True,
                "test_cur_record_exposed": False,
                "trade_day_visible_news": trade_day_receipt,
            },
            "timing": {
                "day_step": {
                    "started_at_utc": day_started.isoformat(),
                    "finished_at_utc": day_finished.isoformat(),
                    "elapsed_seconds": elapsed,
                },
                "phase_cumulative": {
                    "warmup_elapsed_seconds": state["warmup"]["elapsed_seconds"],
                    "test_elapsed_seconds": cumulative_test_elapsed,
                    "combined_elapsed_seconds": state["warmup"]["elapsed_seconds"] + cumulative_test_elapsed,
                },
            },
            "llm_usage": {
                "provider": manifest["model"]["provider"],
                "model": manifest["model"]["llm"],
                "day": day_usage,
                "cumulative_test": cumulative_test_usage,
                "cumulative_total_including_warmup": cumulative_total_usage,
                "day_audit": day_usage_audit,
                "cumulative_test_audit": cumulative_test_usage_audit,
                "cumulative_total_including_warmup_audit": cumulative_total_usage_audit,
                "usage_source": day_usage_audit["usage_source"],
                "estimated_day_cost_usd": _estimate_cost(
                    day_usage,
                    input_price_per_mtok,
                    output_price_per_mtok,
                ),
                "estimated_total_cost_usd": _estimate_cost(
                    cumulative_total_usage,
                    input_price_per_mtok,
                    output_price_per_mtok,
                ),
                "cost_note": (
                    "estimated from input/output price flags; cache token fields are tracked separately and excluded from the estimate"
                    if input_price_per_mtok is not None and output_price_per_mtok is not None
                    else "not estimated; pass both price flags to compute USD cost"
                ),
            },
            "decision": {
                "raw": raw_decision,
                "native_action": raw_decision,
                "action_semantics": "incremental_position_step",
                "reason": raw_reflection.get("summary_reason"),
            },
            "portfolio": {
                "position_units_before_decision": position_units_before_decision,
                "position_before_decision": _position_sign(position_units_before_decision),
                "action_direction": action_direction,
                "position_units_after_decision": position_units_after_decision,
                "position_after_decision": _position_sign(position_units_after_decision),
            },
            "artifacts": {
                "raw_reflection": str(raw_reflection_path),
            },
        }
        _write_json_atomic(day_dir / "audit.json", audit)

        checkpoint_dir = _save_agent_env_checkpoint(
            checkpoint_root=test_checkpoint_root,
            committed_day=trade_date,
            agent=test_agent,
            env=test_env,
        )

        next_state = copy.deepcopy(state)
        next_state["test"]["last_committed_day"] = trade_date
        next_state["test"]["completed_count"] += 1
        next_state["test"]["llm_usage"] = cumulative_test_usage
        next_state["test"]["elapsed_seconds"] = cumulative_test_elapsed
        if next_state["test"]["completed_count"] == len(test_days):
            next_state["phase"] = "complete"
        _write_state(resolved_output_dir, next_state)
        state = next_state

        audit["artifacts"]["checkpoint_dir"] = str(checkpoint_dir)
        _write_json_atomic(day_dir / "audit.json", audit)
        print(
            "  "
            f"{trade_date} decision={raw_decision} "
            f"position={audit['portfolio']['position_after_decision']} "
            f"elapsed={elapsed:.1f}s "
            f"llm_calls={day_usage['llm_calls']} "
            f"tokens_in={day_usage['tokens_in']} "
            f"tokens_out={day_usage['tokens_out']}"
        )

    summary_path = _write_summary(resolved_output_dir, manifest, state)
    _write_cost_reports(
        output_dir=resolved_output_dir,
        manifest=manifest,
        state=state,
    )
    print(f"FinMem GCUSD audit summary written to {summary_path}")
    return summary_path
