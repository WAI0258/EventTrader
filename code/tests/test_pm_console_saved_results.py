"""Saved sample mode must not recalculate from raw bars or allow writes."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from event_trader.storage import build_workspace_layout
from event_trader.tools import pm_console_server as console


@pytest.fixture
def sample_server(tmp_path, monkeypatch):
    layout = build_workspace_layout(tmp_path)
    monkeypatch.setattr(console, "list_pm_console_targets", lambda _layout: ("sox",))

    def reject_recalculation(**kwargs):
        raise AssertionError("Saved results attempted market recalculation")

    monkeypatch.setattr(console, "_build_position_response_for_target", reject_recalculation)
    handler = console._build_handler(
        layout, runtime_mode="unknown", market_data_snapshot_id=None,
        display_start_at=None, saved_results=True,
        buy_hold_results_dir=tmp_path / "local",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield layout, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_returns_saved_curves_without_recalculation(sample_server):
    layout, base = sample_server
    path = layout.runtime_root / "pm_console/sox.position.json"
    path.parent.mkdir(parents=True)
    expected = {"target_key": "sox", "points": [{"pm_pipeline_value": 1.1}]}
    path.write_text(json.dumps(expected), encoding="utf-8")
    with urlopen(base + "/api/targets/sox/position") as response:
        assert json.load(response) == expected


def test_missing_results_do_not_fall_back_to_market_calculation(sample_server):
    _, base = sample_server
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/targets/sox/position")
    assert error.value.code == 400


def test_rejects_wrong_target_identity(sample_server):
    layout, base = sample_server
    path = layout.runtime_root / "pm_console/sox.position.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"target_key":"btc"}', encoding="utf-8")
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/targets/sox/position")
    assert error.value.code == 400


def test_api_adds_local_baseline_without_changing_saved_file(sample_server):
    import hashlib

    layout, base = sample_server
    path = layout.runtime_root / "pm_console/sox.position.json"
    path.parent.mkdir(parents=True)
    expected = {"target_key": "sox", "market_symbol": "SOXX", "lines": [], "notes": [],
                "points": [{"time": "2026-01-05T15:00:00+00:00",
                            "pm_pipeline_value": 1.1, "analysis_direct_value": 0.9}]}
    sample = json.dumps(expected).encode()
    path.write_bytes(sample)
    overlay = {"sample_sha256": hashlib.sha256(sample).hexdigest(),
               "target_key": "sox", "market_symbol": "SOXX", "provider": "futu_openapi",
               "entry_cost_bps": 1.5,
               "points": [{"time": expected["points"][0]["time"], "value": 1.02}]}
    local = layout.root / "local"
    local.mkdir()
    overlay_path = local / "sox.buy-hold.json"
    overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
    with urlopen(base + "/api/targets/sox/position") as response:
        actual = json.load(response)
    assert actual["points"][0] == expected["points"][0] | {"buy_hold_value": 1.02}
    assert actual["lines"][-1]["key"] == "buy_hold"
    assert path.read_bytes() == sample
    overlay["sample_sha256"] = "outdated"
    overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/targets/sox/position")
    assert error.value.code == 400


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_saved_mode_rejects_writes_and_model_requests(sample_server, method):
    _, base = sample_server
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + "/api/targets/sox/operator", data=b"{}", method=method))
    assert error.value.code == 405
