# PM Console Frontend

Local Vite React TypeScript UI for the event-trader PM Console.

Implemented routes:

```text
/targets/:target/position
/targets/:target/pm
/targets/:target/thesis
```

The frontend talks only to the local PM Console API. It must not read workspace
files directly.

## Backend API

Start the read-only API server from the repository root:

`--config` is optional. When omitted, the server resolves the matching
repo-local `config/kernel.<mode>.<target>.toml` from the workspace. When
provided, that kernel config is used directly. This config also supplies the
optional reader aids: Thesis Translation and Quick Brief read the selected
workspace config's existing OpenAI-compatible `[checker_agent]` settings.

```powershell
event-trader-pm-console `
  --workspace-root .local/live-sox-workspace `
  --host 127.0.0.1 `
  --port 8765
```

To inspect several isolated targets from one server, use a persistent
config-only mount catalog. Switching the target sidebar changes the read scope
without a restart:

```powershell
@'
version = 1

[[mount]]
config = "../config/kernel.live.sox.toml"

[[mount]]
config = "../config/kernel.live.gold.event-trader-agents.toml"

[[mount]]
config = "../config/kernel.live.btc.toml"

# Add MRNA after creating its kernel config:
# [[mount]]
# config = "../config/kernel.live.mrna.toml"
'@ | Set-Content .local/pm-console.mounts.toml

event-trader-pm-console `
  --mount-catalog .local/pm-console.mounts.toml `
  --host 127.0.0.1 `
  --port 8765
```

Each `[[mount]]` contains only a config path. Target key, workspace, runtime mode,
and shared market-data root are derived from and validated against the config.
Catalog changes are atomically reloaded per request; invalid intermediate saves
keep the last valid target set and are reported by `/api/targets`. The server is
read-only and does not supervise target runtimes.

Set the key already required by the selected kernel config through the existing
environment or repository `.env` loading. No new reader flags, key names, or
config format are required. Reader requests use the generic OpenAI-compatible
payload and do not add provider-specific thinking controls.

When `[checker_agent]` is absent, is not an OpenAI-compatible configuration, or
its configured key is unavailable, Thesis Translation and Quick Brief report an
explicit unavailable state. Position Management, PM Evolution, Thesis
Evolution, context, and all other read-only pages remain usable from the
workspace. The UI never renders leading `<think>...</think>` blocks from a
reader response.

Implemented endpoints:

```text
GET /api/targets
GET /api/targets/{target}/context
GET /api/targets/{target}/position
GET /api/targets/{target}/pm-decisions
GET /api/targets/{target}/thesis-revisions
```

Override the API base URL when needed:

```powershell
$env:VITE_PM_CONSOLE_API_BASE_URL = "http://127.0.0.1:8765"
```

## Frontend Commands

```powershell
npm.cmd --prefix frontend/pm-console run typecheck
npm.cmd --prefix frontend/pm-console run build
npm.cmd --prefix frontend/pm-console run dev
```

PowerShell may block the `npm` shim on some machines; use `npm.cmd`.

## Boundary

The Position Management page renders API DTOs from `src/event_trader/pm_console`.
The underlying read model is owned by `src/event_trader/position_monitoring`.

Do not add durable stores, direct workspace reads, auth, user management, or
external product-shell concerns to this frontend.
