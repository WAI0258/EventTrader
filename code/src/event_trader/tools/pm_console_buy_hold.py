"""Build a local Buy & Hold overlay from the user's configured market provider."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from math import isfinite
from pathlib import Path

from event_trader.config import load_kernel_config, resolve_execution_buy_cost_bps_for_target
from event_trader.contracts.view_state_change import MarketDataSeries
from event_trader.market.provider import build_market_bars_provider
from event_trader.position_monitoring.performance import (
    TargetWeightEvent,
    calculate_cumulative_equity_series,
)
from event_trader.validation.market_mapping import resolve_market_mapping


def build_overlay(
    *, sample: bytes, series: MarketDataSeries, provider: str, entry_cost_bps: float
) -> dict:
    payload = json.loads(sample)
    if not isfinite(entry_cost_bps) or entry_cost_bps < 0:
        raise ValueError("Entry cost must be finite and non-negative.")
    times = [datetime.fromisoformat(point["time"]) for point in payload["points"]]
    bars = tuple(bar for bar in series.bars if bar.start_at >= times[0] and bar.end_at <= times[-1])
    if not bars or bars[0].start_at != times[0] or bars[-1].end_at != times[-1]:
        raise ValueError("Provider does not cover the sample's exact start and end.")
    series = MarketDataSeries(bars=bars)
    curve = calculate_cumulative_equity_series(
        market_data=series,
        events=(
            TargetWeightEvent(
                effective_at=bars[0].start_at,
                target_weight=1.0,
                state="strong_long",
                execution_price=bars[0].open_price * (1.0 + entry_cost_bps / 10_000.0),
            ),
        ),
        base_value=payload["base_value"],
    )
    values = {point.time: point.value for point in curve}
    if set(values) != set(times):
        raise ValueError(
            "Provider bar boundaries differ from the saved sample; "
            "check instrument, session, granularity and historical coverage."
        )
    return {
        "target_key": payload["target_key"],
        "sample_sha256": hashlib.sha256(sample).hexdigest(),
        "provider": provider,
        "market_symbol": payload["market_symbol"],
        "entry_cost_bps": entry_cost_bps,
        "points": [
            {"time": point["time"], "value": values[time]}
            for point, time in zip(payload["points"], times, strict=True)
        ],
    }


def apply_overlay(*, payload: dict, sample: bytes, overlay: dict) -> dict:
    if (
        overlay["sample_sha256"] != hashlib.sha256(sample).hexdigest()
        or overlay["target_key"] != payload["target_key"]
        or overlay["market_symbol"] != payload["market_symbol"]
        or [point["time"] for point in overlay["points"]]
        != [point["time"] for point in payload["points"]]
    ):
        raise ValueError("Buy & Hold overlay does not match this saved sample.")
    for point, baseline in zip(payload["points"], overlay["points"], strict=True):
        if (
            not isinstance(baseline["value"], int | float)
            or not isfinite(baseline["value"])
            or baseline["value"] <= 0
        ):
            raise ValueError("Buy & Hold overlay values must be finite and positive.")
        point["buy_hold_value"] = baseline["value"]
    explanation = (
        f"User-fetched {overlay['provider']} / {overlay['market_symbol']}; "
        f"same saved interval, one entry cost of {overlay['entry_cost_bps']} bps. "
        "PM and Direct remain saved results; provider prices may differ."
    )
    payload["lines"] = [line for line in payload["lines"] if line["key"] != "buy_hold"]
    payload["lines"].append(
        {
            "key": "buy_hold",
            "label": "Buy & Hold",
            "role": "baseline",
            "status": {"code": "ready", "explanation": explanation},
            "explanation": explanation,
        }
    )
    payload["notes"].append(explanation)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(".local/buy-hold"))
    args = parser.parse_args(argv)
    config = load_kernel_config(args.config)
    mapping = resolve_market_mapping(config, args.target)
    sample = (
        config.workspace_root / "runtime" / "pm_console" / f"{mapping.target_key}.position.json"
    ).read_bytes()
    payload = json.loads(sample)
    if (
        payload["target_key"] != mapping.target_key
        or payload["market_symbol"] != mapping.market_symbol
    ):
        raise ValueError("Config instrument does not match the saved sample.")
    if config.validation is None:
        raise ValueError("Market-data configuration is required.")
    provider_name = config.validation.market_data.provider
    if mapping.target_key == "btc" and provider_name not in {"hyperliquid_perp", "local_archive"}:
        raise ValueError(
            "The BTC sample is a Hyperliquid perpetual; a spot baseline is not equivalent."
        )
    provider = build_market_bars_provider(config, provider_name=provider_name)
    series = provider.read_series(
        mapping,
        start_at=datetime.fromisoformat(payload["points"][0]["time"]),
        end_at=datetime.fromisoformat(payload["points"][-1]["time"]),
    )
    overlay = build_overlay(
        sample=sample,
        series=series,
        provider=provider_name,
        entry_cost_bps=resolve_execution_buy_cost_bps_for_target(
            config.execution, target_key=mapping.target_key
        ),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{mapping.target_key}.buy-hold.json"
    output.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    print(f"Buy & Hold overlay saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
