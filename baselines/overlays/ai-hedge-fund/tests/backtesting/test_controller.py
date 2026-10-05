from src.backtesting.controller import AgentController


def dummy_agent(**kwargs):
    # Echo basic structure with only one decision
    tickers = kwargs["tickers"]
    return {
        "decisions": {tickers[0]: {"action": "buy", "quantity": "10"}},
        "analyst_signals": {"agentA": {tickers[0]: {"signal": "bullish"}}},
    }


def audit_agent(**kwargs):
    tickers = kwargs["tickers"]
    return {
        "decisions": {tickers[0]: {"action": "hold", "quantity": 0}},
        "analyst_signals": {},
        "audit": {
            "llm_usage": {
                "llm_calls": 2,
                "tokens_in": 10,
                "tokens_out": 4,
                "tokens_total": 14,
                "usage_observed": True,
                "calls_missing_usage": 0,
                "per_agent_calls": {"portfolio_manager": 2},
            }
        },
    }


def test_agent_controller_normalizes_and_snapshots(portfolio):
    ctrl = AgentController()
    out = ctrl.run_agent(
        dummy_agent,
        tickers=["AAPL", "MSFT"],
        start_date="2024-01-01",
        end_date="2024-01-10",
        portfolio=portfolio,
        model_name="m",
        model_provider="p",
        selected_analysts=["x"],
    )

    # Decisions normalized for all tickers
    assert out["decisions"]["AAPL"]["action"] == "buy"
    assert out["decisions"]["AAPL"]["quantity"] == 10.0
    # Missing ticker defaults to hold/0
    assert out["decisions"]["MSFT"]["action"] == "hold"
    assert out["decisions"]["MSFT"]["quantity"] == 0.0
    # Analyst signals are passed through
    assert "agentA" in out["analyst_signals"]


def test_agent_controller_preserves_audit_payload(portfolio):
    ctrl = AgentController()
    out = ctrl.run_agent(
        audit_agent,
        tickers=["AAPL"],
        start_date="2024-01-01",
        end_date="2024-01-10",
        portfolio=portfolio,
        model_name="m",
        model_provider="p",
        selected_analysts=["x"],
    )

    assert out["audit"]["llm_usage"]["llm_calls"] == 2
    assert out["audit"]["llm_usage"]["tokens_total"] == 14


