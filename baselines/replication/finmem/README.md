# FinMem replication

This directory contains the retained FinMem reproduction evidence and the
paper-compatible runner used to produce it.

## Retained evidence

The authoritative completed TSLA run is:

`runs/paper_compat_finsaber_finmem_tsla_20221006_20230410_m2_1_qinzhi_v1`

Its challenge-facing interpretation, source comparison, metrics, and artifact
map are in [evidence/FINMEM_FINSABER_REPLICATION_EVIDENCE.md](evidence/FINMEM_FINSABER_REPLICATION_EVIDENCE.md).

The prepared FINSABER inputs are retained under
`../data/finmem_tsla_paper/finsaber_finmem/`; their upstream legacy pickle is
under `../data/finmem_tsla_paper/benchmark/`.

## Re-run

The runner requires the isolated Python 3.10 environment, `MINIMAX_API_KEY`,
and `qinzhi_embed` in the repository `.env`:

```powershell
.\FinMem-LLM-StockTrading\.venv-paper-compat\Scripts\python.exe `
  .\replication\finmem\paper_compat\run_finsaber.py
```

It resumes only when the run manifest and inputs match. The launcher refuses
to run if the vendored `FinMem-LLM-StockTrading/puppy` package is modified.

## GCUSD baseline

The gold baseline uses the same paper-compatible bootstrap, M2.1 transport,
Qinzhi embedding path, original FinMem core, daily EOD visibility guard, and
daily checkpoint semantics as the retained TSLA evidence run. It reads
`MINIMAX_API_KEY_1d` and `qinzhi_embed` from the repository `.env`.

```powershell
$env:PYTHONUTF8 = "1"
.\FinMem-LLM-StockTrading\.venv-paper-compat\Scripts\python.exe `
  .\replication\finmem\paper_compat\run_gcusd.py
```

The default output directory is
`experiments/finmem_gcusd_paper_compat_20260101_20260630_m2_1_qinzhi_v1`.
Repeat the same command after interruption to resume from the last committed
day. It intentionally creates a new result directory and does not reuse the
retained legacy gold run.

## Ownership boundary

- `FinMem-LLM-StockTrading/puppy/` remains the vendored FinMem implementation.
- `paper_compat/` contains only process-local provider transport, environment
  setup, and launch logic for M2.1/Qinzhi.
- `finmem_tsla_runner.py`, `build_finmem_tsla_env.py`,
  `prepare_finsaber_finmem_inputs.py`, and `evaluate_finsaber_finmem.py` own
  deterministic input preparation, resume/audit records, and reported metrics.

The retained run's raw reflection, per-day audit, checkpoint, input receipt,
and token usage are all under its own `runs/` directory. Do not use generated
vendor logs as evidence because they mix invocations; use the per-run artifacts.
