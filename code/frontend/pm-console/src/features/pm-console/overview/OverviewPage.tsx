import { ArrowUpRight, Filter, LayoutDashboard } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type {
  PMConsoleOverviewReadiness,
  PMConsoleOverviewTarget,
} from "../../../types";
import { usePmConsoleOverviewQuery } from "../hooks";
import { useLocale } from "../locale/LocaleProvider";

type OverviewFilter = "all" | PMConsoleOverviewReadiness;

export function OverviewPage() {
  const { t } = useLocale();
  const query = usePmConsoleOverviewQuery();
  const [filter, setFilter] = useState<OverviewFilter>("all");
  const [search, setSearch] = useState("");

  const targets = useMemo(() => {
    const normalizedSearch = search.trim().toLowerCase();
    return (query.data?.targets ?? []).filter((target) => {
      const matchesFilter = filter === "all" || target.readiness.code === filter;
      const matchesSearch =
        !normalizedSearch ||
        target.target_key.toLowerCase().includes(normalizedSearch) ||
        target.workspace_name.toLowerCase().includes(normalizedSearch);
      return matchesFilter && matchesSearch;
    });
  }, [filter, query.data?.targets, search]);

  if (query.isLoading) return <div className="route-empty">{t("Loading PM Overview...")}</div>;
  if (query.isError) {
    return (
      <div className="route-empty">
        <p>{t("PM Overview could not be loaded from the local API.")}</p>
      </div>
    );
  }

  const payload = query.data;
  if (!payload) return <div className="route-empty">{t("No overview data is available.")}</div>;
  return (
    <div className="overview-page">
      <section className="hero-card overview-hero-card">
        <div>
          <p className="eyebrow">{t("Cross-workspace current-action board")}</p>
          <h2>{t("PM Overview")}</h2>
          <p className="hero-copy">
            {t("A compact view of persisted target state, the latest PM obligation, and setup readiness.")}
          </p>
        </div>
        <div className="overview-hero-meta">
          <div className="overview-api-status">
            <LayoutDashboard size={17} aria-hidden="true" />
            <div>
              <span>{t("API")}</span>
              <strong>{t("Online")}</strong>
            </div>
          </div>
          <div className="overview-count">
            <span>{t("Mounted targets")}</span>
            <strong>{payload.mounted_target_count}</strong>
          </div>
        </div>
      </section>

      <section className="overview-toolbar" aria-label={t("Overview filters")}>
        <div className="overview-filter-group" role="group" aria-label={t("Readiness filter")}>
          <Filter size={15} aria-hidden="true" />
          {(["all", "action_required", "incomplete", "ready", "unavailable"] as const).map(
            (value) => (
              <button
                key={value}
                className={filter === value ? "overview-filter active" : "overview-filter"}
                type="button"
                onClick={() => setFilter(value)}
              >
                {filterLabel(value)}
              </button>
            ),
          )}
        </div>
        <label className="overview-search">
          <span className="sr-only">{t("Filter targets")}</span>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("Filter targets")}
            type="search"
          />
        </label>
      </section>

      {payload.reload_error ? (
        <p className="overview-notice">{t("Mount catalog reload issue:")} {payload.reload_error}</p>
      ) : null}

      <section className="overview-target-list" aria-label={t("Mounted target overview")}>
        {targets.length ? (
          targets.map((target) => <OverviewTargetRow key={target.target_key} target={target} />)
        ) : (
          <div className="side-panel overview-empty">
            <p>{t("No targets match the current filter.")}</p>
          </div>
        )}
      </section>
    </div>
  );
}

function OverviewTargetRow({ target }: { target: PMConsoleOverviewTarget }) {
  const { t } = useLocale();
  return (
    <article className="side-panel overview-target-row">
      <div className="overview-target-main">
        <div className="overview-target-heading">
          <div>
            <p className="eyebrow">{target.workspace_name}</p>
            <h3>{target.target_key.toUpperCase()}</h3>
          </div>
          <ReadinessPill status={target.readiness.code as PMConsoleOverviewReadiness} />
        </div>
        <p className="overview-workspace-path" title={target.workspace_root}>
          {target.workspace_root}
        </p>
        <div className="overview-fact-grid">
          <Fact label={t("Runtime")} value={formatRuntimeMode(target.runtime_mode)} />
          <Fact
            label={t("Current exposure")}
            value={
              target.current_portfolio_state
                ? `${formatState(target.current_portfolio_state.state)} · ${formatWeight(
                    target.current_portfolio_state.target_weight,
                  )}`
                : t("Not persisted")
            }
          />
          <Fact
            label={t("Latest PM")}
            value={
              target.latest_pm_decision
                ? `${formatState(target.latest_pm_decision.requested_state)} · ${formatWeight(
                    target.latest_pm_decision.requested_target_weight,
                  )}`
                : t("None persisted")
            }
          />
          <Fact label={t("Execution")} value={formatExecution(target.execution_status.code)} />
          <Fact
            label={t("Thesis")}
            value={target.latest_thesis_revision ? formatDate(target.latest_thesis_revision.business_at) : t("None persisted")}
          />
          <Fact label={t("Operator")} value={target.operator_status.code} />
        </div>
        {target.problem_codes.length ? (
          <p className="overview-problems">{target.problem_codes.map(formatProblem).join(" · ")}</p>
        ) : null}
      </div>
      <Link
        className="overview-target-link"
        to={targetPath(target.target_key, target.default_view)}
        aria-label={
          t("Open") === "Open"
            ? `Open ${target.target_key} ${target.default_view} view`
            : `${t("Open")} ${target.target_key} ${viewLabel(target.default_view, t)} ${t("view")}`
        }
      >
        <span>{t("Open")} {viewLabel(target.default_view, t)}</span>
        <ArrowUpRight size={16} aria-hidden="true" />
      </Link>
    </article>
  );
}

function ReadinessPill({ status }: { status: PMConsoleOverviewReadiness }) {
  return <span className={`overview-readiness ${status}`}>{readinessLabel(status)}</span>;
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="overview-fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function targetPath(target: string, view: string) {
  return `/targets/${encodeURIComponent(target)}/${view}`;
}

function filterLabel(value: OverviewFilter) {
  return value === "all" ? "All" : readinessLabel(value);
}

function readinessLabel(value: PMConsoleOverviewReadiness) {
  return value === "action_required"
    ? "Action required"
    : value[0].toUpperCase() + value.slice(1);
}

function viewLabel(value: string, t: (source: string) => string) {
  return value === "pm" ? t("PM Trajectory") : value === "thesis" ? t("Thesis Evolution") : value === "position" ? t("Position Management") : value === "episodes" ? t("Episodes") : value === "operator" ? t("Operator") : value;
}

function formatRuntimeMode(value: string) {
  return value === "catchup" ? "Catch-up" : value[0].toUpperCase() + value.slice(1);
}

function formatExecution(value: string) {
  return value === "no_op" ? "No-op" : value.replaceAll("_", " ");
}

function formatState(value: string | null) {
  return value ? value.replaceAll("_", " ") : "Unavailable";
}

function formatWeight(value: number | null) {
  return value === null ? "Unavailable" : `${(value * 100).toFixed(0)}%`;
}

function formatDate(value: string) {
  return value.replace("T", " ").replace("+00:00", " UTC");
}

function formatProblem(value: string) {
  return value.replaceAll("_", " ");
}
