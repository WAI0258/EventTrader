"""Offline checks for the exported paper artifact; no third-party packages."""
import ast
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "baselines/experiments/gold_baseline_eval_20260101_20260630"


def public_files():
    # A cloned repository can contain Git metadata and a local runtime environment.
    ignored = {".git", ".venv", "__pycache__", ".local", ".pytest_cache", ".ruff_cache"}
    return [p for p in ROOT.rglob("*") if p.is_file()
            and not any(part in ignored for part in p.relative_to(ROOT).parts)
            and p.name != ".env"]


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def metrics(points, equity_key):
    points = sorted(points, key=lambda r: r["trade_date"])
    values = [float(r[equity_key]) for r in points]
    previous = peak = 100000.0
    returns, worst = [], 0.0
    for value in values:
        returns.append(value / previous - 1)
        previous = value
        peak = max(peak, value)
        worst = max(worst, 1 - value / peak)
    return {"return": values[-1] / 100000 - 1, "mdd": worst, "days": len(values)}


def main():
    checks, errors = [], []

    def check(label, ok, details=""):
        checks.append({"check": label, "passed": bool(ok), "details": details})
        if not ok:
            errors.append(label)

    python_files = [p for p in public_files() if p.suffix == ".py"]
    syntax_errors = []
    for p in python_files:
        try:
            ast.parse(p.read_text(encoding="utf-8-sig"), filename=p.relative_to(ROOT).as_posix())
        except (SyntaxError, UnicodeError) as exc:
            syntax_errors.append(f"{p.relative_to(ROOT)}: {exc}")
    check("Python syntax", not syntax_errors, f"{len(python_files)} files; {syntax_errors}")
    grouped = defaultdict(list)
    for row in rows(EVAL / "eventtrader_vs_baselines_progress.csv"):
        grouped[row["strategy"]].append(row)
    reported = {"event_trader": (37.68, 10.34), "tradingagents": (18.97, 11.64), "finmem": (11.72, 12.28), "ai_hedge_fund": (2.96, 19.74), "buy_and_hold": (-6.96, 25.30)}
    main_metrics = {}
    for label, (ret, dd) in reported.items():
        value = metrics(grouped[label], "equity_close")
        main_metrics[label] = value
        check(f"Main curve: {label}", abs(value["return"]*100-ret) < .0051 and abs(value["mdd"]*100-dd) < .0051, str(value))
    native = {r["baseline"]: r for r in rows(EVAL / "trading_summary.csv")}
    factor = 154 / 181 * 365
    finmem = native["finmem"]
    scaled = {key: float(finmem[key]) * math.sqrt(factor / 252) for key in ["sharpe", "sortino"]}
    check("FinMem common-calendar rescaling", round(scaled["sharpe"], 3) == .918 and round(scaled["sortino"], 3) == .852, str(scaled))
    tb = ROOT / "experiments/timebatch"
    variants = defaultdict(list)
    for row in rows(tb / "daily_equity.csv"):
        variants[row["variant"]].append(row)
    for row in rows(tb / "summary.csv"):
        value = metrics(variants[row["variant"]], "equity")
        check(f"Timebatch curve: {row['variant']}", abs(value["return"]-float(row["total_return"])) < 1e-8 and abs(value["mdd"]-float(row["max_drawdown"])) < 1e-8, str(value))
    ab = ROOT / "experiments/ceau_ablation"
    reported_ab = {r["path"]: r for r in rows(ab / "manuscript_reported.csv")}
    cost = {}
    for label, expected in reported_ab.items():
        triggers = rows(ab / f"{label}_triggers.csv")
        check(f"Ablation triggers: {label}", len({r["work_item_id"] for r in triggers}) == int(expected["triggers"]), f"{len(triggers)} invocations")
        analyses = rows(ab / f"{label}_successful_analyses.csv")
        included = [r for r in analyses if r["matched_successful_log"] == "True"]
        seconds = sum(float(r["runtime_seconds"]) for r in included)
        tokens = sum(int(r["total_tokens"]) for r in included)
        cost[label] = (seconds, tokens)
        check(f"Ablation cost: {label}", len(included) == len(analyses) and round(seconds/len(included), 1) == float(expected["mean_runtime_seconds"]) and round(seconds/3600, 2) == float(expected["total_runtime_hours"]) and round(tokens/1e6, 2) == float(expected["tokens_millions"]), f"{len(included)} calls; {seconds}s; {tokens} tokens")
        states = sorted(rows(ab / f"{label}_assessments.csv"), key=lambda r: (r["business_at"], r["assessment_id"]))
        repeated = changes = 0
        previous = "flat"
        for row in states:
            if row["as_if_flat_state"] == previous:
                repeated += 1
            else:
                changes += 1
            previous = row["as_if_flat_state"]
        check(f"Ablation directional states: {label}", len(states) == int(expected["assessments"]) and repeated == int(expected["repeated_states"]) and changes == int(expected["state_changes"]), f"{len(states)} assessments; {repeated} repeated; {changes} changes")
    reductions = [(1-cost['ceau'][i]/cost['per_record'][i])*100 for i in [0, 1]]
    check("CEAU aggregate reductions", round(reductions[0], 1) == 64.2 and round(reductions[1], 1) == 62.9, str(reductions))
    report = {"passed": not errors, "checks": checks, "not_verified": ["full model runs", "raw input redistribution rights", "exact per-run code/config revision", "LaTeX compilation", "CEAU trading outcomes from raw prices", "all headline runtime metrics from excluded logs", "full model-runtime dependency installation"]}
    print(json.dumps(report, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
