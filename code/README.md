# Event-Trader research source

This directory contains the development implementation exported from source revision 7abc566916e1db93477742ccf74c3085905ec6c5, including the original PM Console frontend and backend. The paper-era public snapshot remains in the paper-v1 tag. Start at the repository README and REPRODUCTION.md for saved-result checks and reproduction limitations.

The main flow admits timestamped evidence, checks relevance, forms CEAU analysis units, updates research memory, and routes research changes to portfolio review and simulated execution. Source visibility and deterministic replay boundaries are maintained separately from model reasoning.

Core environment (Python >=3.13): `uv sync --frozen --no-dev` from this directory. The MiroThinker model runtime has a separate dependency environment under `vendor/mirothinker/apps/miroflow-agent/`; inspect its pyproject and lockfile before installation. Full runs need both core and model-runtime dependencies. No model credentials are shipped.

The vendored runtime is already present as ordinary files, not a submodule. Do not run git submodule commands for this export. See MIROTHINKER_VENDOR_PINNING.md for its recorded revision. Commands and paths referring to excluded input archives require inputs described in ../REPRODUCTION.md.

For the supplied paper-trading examples, run `event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results`. The native interface is in `frontend/pm-console/`. This read-only sample mode needs no model credentials or raw market database.
