# TradingAgents 2024Q1 Reproduction

Isolated paper-window reproduction; shared inputs live under
`replication/data/tradingagents_paper_2024q1`.

- Evaluation: 2024-01-01 through 2024-03-29.
- Price: frozen Futu QFQ daily bars; 2023 is warm-up only.
- News: frozen `jackzhousmu/news` records.
- Model: MiniMax M2.1 for deep and quick roles.
- Initial controlled run: market and news analysts.
- Historical fundamentals and Reddit/social inputs are omitted, not replaced
  with current data.

This is a partial paper-window reproduction, not an exact replay of the
paper's unavailable private data snapshot.

## Post-hoc execution adapters

After a completed run, evaluate the immutable report stream with two
predeclared deterministic adapters:

```powershell
..\..\tradingagents\.venv\Scripts\python.exe .\evaluate_completed_run.py
```

Outputs under `runs/<run>/execution/` include a daily `mapping_audit.csv`, a
text-faithful long-only adapter (Underweight only reduces an existing long),
and a fixed-notional directional sensitivity proxy (Trader
Buy/Hold/Sell = +100%/maintain/-100%). They start with 100,000 cash, execute
at the next trading-day open, apply reported stops against same-day OHLC, and
use zero cost because the paper does not disclose cost assumptions. They are
evaluation adapters, not the paper's unavailable execution code.

```powershell
..\..\tradingagents\.venv\Scripts\python.exe .\run_reproduction.py --dry-run
..\..\tradingagents\.venv\Scripts\python.exe .\run_reproduction.py --dates 2024-01-02
```

Completed dates are skipped automatically.
