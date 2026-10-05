from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import mean
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINMEM_DIR = (
    WORKSPACE_ROOT / "experiments" / "finmem_gcusd_full_20260101_20260630_processed_news_cn_v2"
)
DEFAULT_AIHF_DIR = (
    WORKSPACE_ROOT / "experiments" / "ai_hedge_fund_gcusd_full_20260101_20260630"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _position_sign(position_units: int) -> str:
    if position_units > 0:
        return "long"
    if position_units < 0:
        return "short"
    return "flat"


def _finmem_step_direction(raw_action: str | None) -> int:
    mapping = {"buy": 1, "hold": 0, "sell": -1}
    return mapping.get((raw_action or "").lower(), 0)


def _iter_day_audit_paths(days_dir: Path) -> list[Path]:
    return sorted(
        (
            day_dir / "audit.json"
            for day_dir in days_dir.iterdir()
            if day_dir.is_dir() and (day_dir / "audit.json").exists()
        ),
        key=lambda path: path.parent.name,
    )


def _write_finmem_cost_markdown(cost_report_path: Path, cost_report: dict[str, Any]) -> None:
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
    cost_report_path.with_suffix(".md").write_text(markdown + "\n", encoding="utf-8")


def backfill_finmem(experiment_dir: Path) -> None:
    audit_paths = _iter_day_audit_paths(experiment_dir / "days")
    cumulative_units = 0
    audits: list[dict[str, Any]] = []

    for audit_path in audit_paths:
        audit = _read_json(audit_path)
        decision = audit.setdefault("decision", {})
        raw_action = decision.get("raw")
        step_direction = _finmem_step_direction(raw_action)
        position_units_before = cumulative_units
        cumulative_units += step_direction
        portfolio = audit.setdefault("portfolio", {})

        decision["raw"] = raw_action
        decision["native_action"] = raw_action
        decision["action_semantics"] = "incremental_position_step"
        decision.pop("normalized_position", None)

        portfolio["position_units_before_decision"] = position_units_before
        portfolio["position_before_decision"] = _position_sign(position_units_before)
        portfolio["action_direction"] = step_direction
        portfolio["position_units_after_decision"] = cumulative_units
        portfolio["position_after_decision"] = _position_sign(cumulative_units)

        _write_json_atomic(audit_path, audit)
        audits.append(audit)

    summary_path = experiment_dir / "summary.json"
    summary = _read_json(summary_path)
    summary["runs"] = audits
    _write_json_atomic(summary_path, summary)

    cost_report_path = experiment_dir / "cost_report.json"
    cost_report = _read_json(cost_report_path)
    slowest = sorted(
        (
            {
                "trade_date": audit["trade_date"],
                "elapsed_seconds": float(audit["timing"]["day_step"]["elapsed_seconds"]),
                "native_action": audit["decision"].get("native_action", audit["decision"].get("raw")),
                "position_after_decision": audit.get("portfolio", {}).get("position_after_decision", "unknown"),
            }
            for audit in audits
        ),
        key=lambda item: item["elapsed_seconds"],
        reverse=True,
    )[:5]
    cost_report["slowest_decisions"] = slowest
    _write_json_atomic(cost_report_path, cost_report)
    _write_finmem_cost_markdown(cost_report_path, cost_report)

    print(f"FinMem backfilled: {experiment_dir} ({len(audits)} audits)")


def _aggregate_ahf_usage(audits: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = {
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "usage_observed": False,
        "calls_missing_usage": 0,
        "estimated_cost_usd": 0.0,
    }
    cost_observed = False
    for audit in audits:
        usage = audit.get("llm_usage", {})
        aggregate["llm_calls"] += int(usage.get("llm_calls", 0) or 0)
        aggregate["tokens_in"] += int(usage.get("tokens_in", 0) or 0)
        aggregate["tokens_out"] += int(usage.get("tokens_out", 0) or 0)
        aggregate["tokens_total"] += int(usage.get("tokens_total", 0) or 0)
        aggregate["calls_missing_usage"] += int(usage.get("calls_missing_usage", 0) or 0)
        aggregate["usage_observed"] = aggregate["usage_observed"] or bool(usage.get("usage_observed"))
        if usage.get("estimated_cost_usd") is not None:
            cost_observed = True
            aggregate["estimated_cost_usd"] += float(usage["estimated_cost_usd"])
    if not cost_observed:
        aggregate["estimated_cost_usd"] = None
    return aggregate


def _ahf_position_after_execution(snapshot: dict[str, Any], ticker: str) -> dict[str, Any]:
    position = (snapshot.get("positions") or {}).get(ticker, {})
    long_units = int(position.get("long", 0) or 0)
    short_units = int(position.get("short", 0) or 0)
    net_units = long_units - short_units
    return {
        "long_units": long_units,
        "short_units": short_units,
        "net_units": net_units,
        "position_sign": _position_sign(net_units),
    }


def backfill_aihf(experiment_dir: Path) -> None:
    audit_paths = _iter_day_audit_paths(experiment_dir)
    audits: list[dict[str, Any]] = []

    for audit_path in audit_paths:
        audit = _read_json(audit_path)
        snapshot = audit.get("portfolio_after_execution") or {}
        audit["action_semantics"] = "native_execution_instruction"
        audit["native_action"] = (audit.get("raw_output") or {}).get("action", "hold")
        audit["position_after_execution"] = _ahf_position_after_execution(snapshot, "GCUSD")
        _write_json_atomic(audit_path, audit)
        audits.append(audit)

    aggregate_usage = _aggregate_ahf_usage(audits)

    summary_path = experiment_dir / "summary.json"
    summary = _read_json(summary_path)
    summary["aggregate_llm_usage"] = aggregate_usage
    if audits:
        summary["final_portfolio"] = audits[-1].get("portfolio_after_execution")
    summary["runs"] = audits
    _write_json_atomic(summary_path, summary)

    cost_report_path = experiment_dir / "cost_report.json"
    cost_report = _read_json(cost_report_path)
    cost_report["llm_usage"] = aggregate_usage
    cost_report["run_count"] = len(audits)
    cost_report["filled_count"] = sum(1 for audit in audits if audit.get("status") == "filled")
    _write_json_atomic(cost_report_path, cost_report)

    print(f"AI Hedge Fund backfilled: {experiment_dir} ({len(audits)} audits)")


def main() -> int:
    backfill_finmem(DEFAULT_FINMEM_DIR)
    backfill_aihf(DEFAULT_AIHF_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
