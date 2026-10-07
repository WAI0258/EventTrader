"""User market data may add a baseline without changing saved strategy results."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from event_trader.contracts.view_state_change import MarketDataBar, MarketDataSeries
from event_trader.tools.pm_console_buy_hold import apply_overlay, build_overlay, main


@pytest.fixture
def example():
    start = datetime(2026, 1, 5, 15, tzinfo=UTC)
    bars = tuple(
        MarketDataBar(
            start_at=start + timedelta(minutes=5 * i),
            end_at=start + timedelta(minutes=5 * (i + 1)),
            open_price=100 + 10 * i,
            high_price=110 + 10 * i,
            low_price=100 + 10 * i,
            close_price=110 + 10 * i,
            volume=1,
        )
        for i in range(2)
    )
    payload = {
        "target_key": "sox",
        "market_symbol": "SOXX",
        "base_value": 1.0,
        "lines": [],
        "notes": [],
        "points": [
            {
                "time": (start + timedelta(minutes=5 * i)).isoformat(),
                "pm_pipeline_value": 1 + i / 100,
                "analysis_direct_value": 1 - i / 100,
                "buy_hold_value": None,
            }
            for i in range(3)
        ],
    }
    return json.dumps(payload).encode(), MarketDataSeries(bars=bars)


def test_entry_cost_and_saved_strategy_values(example):
    sample, series = example
    overlay = build_overlay(
        sample=sample, series=series, provider="futu_openapi", entry_cost_bps=10
    )
    payload = apply_overlay(payload=json.loads(sample), sample=sample, overlay=overlay)
    assert [p["buy_hold_value"] for p in payload["points"]] == pytest.approx(
        [1, 1.1 / 1.001, 1.2 / 1.001]
    )
    for old, new in zip(json.loads(sample)["points"], payload["points"], strict=True):
        assert new["pm_pipeline_value"] == old["pm_pipeline_value"]
        assert new["analysis_direct_value"] == old["analysis_direct_value"]


def test_incomplete_provider_window_is_rejected(example):
    sample, series = example
    with pytest.raises(ValueError, match="exact start and end"):
        build_overlay(
            sample=sample,
            series=MarketDataSeries(bars=series.bars[1:]),
            provider="futu_openapi",
            entry_cost_bps=0,
        )


def test_missing_middle_boundary_is_rejected(example):
    sample, series = example
    first, last = series.bars
    merged = MarketDataBar(
        start_at=first.start_at,
        end_at=last.end_at,
        open_price=100,
        high_price=120,
        low_price=100,
        close_price=120,
        volume=2,
    )
    with pytest.raises(ValueError, match="bar boundaries"):
        build_overlay(
            sample=sample,
            series=MarketDataSeries(bars=(merged,)),
            provider="futu_openapi",
            entry_cost_bps=0,
        )


def test_changed_sample_rejects_stale_overlay(example):
    sample, series = example
    overlay = build_overlay(sample=sample, series=series, provider="futu_openapi", entry_cost_bps=0)
    with pytest.raises(ValueError, match="does not match"):
        apply_overlay(payload=json.loads(sample), sample=sample + b" ", overlay=overlay)


def test_cli_reuses_provider_and_keeps_sample_unchanged(example, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from event_trader.tools import pm_console_buy_hold as tool

    sample, series = example
    sample_path = tmp_path / "runtime/pm_console/sox.position.json"
    sample_path.parent.mkdir(parents=True)
    sample_path.write_bytes(sample)
    config = SimpleNamespace(
        workspace_root=tmp_path,
        execution=None,
        validation=SimpleNamespace(market_data=SimpleNamespace(provider="futu_openapi")),
    )
    mapping = SimpleNamespace(target_key="sox", market_symbol="SOXX")
    requests = []

    class Provider:
        def read_series(self, mapping, *, start_at, end_at):
            requests.append((mapping, start_at, end_at))
            return series

    monkeypatch.setattr(tool, "load_kernel_config", lambda _path: config)
    monkeypatch.setattr(tool, "resolve_market_mapping", lambda *_args: mapping)
    monkeypatch.setattr(tool, "build_market_bars_provider", lambda *_args, **_kwargs: Provider())
    monkeypatch.setattr(
        tool, "resolve_execution_buy_cost_bps_for_target", lambda *_args, **_kwargs: 10
    )
    output = tmp_path / "local"
    assert main(["--config", "example.toml", "--target", "sox", "--output-dir", str(output)]) == 0
    assert sample_path.read_bytes() == sample
    assert requests == [(mapping, series.bars[0].start_at, series.bars[-1].end_at)]
    assert json.loads((output / "sox.buy-hold.json").read_bytes())["provider"] == "futu_openapi"
