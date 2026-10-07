import { useMemo } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, ListFilter, Search } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { MarkdownContent } from "../../../components/MarkdownContent";
import type {
  PMConsoleEpisodeDetail,
  PMConsoleEpisodeSummary,
  PMConsoleEpisodeTransition,
} from "../../../types";
import { usePmConsoleEpisodeDetailQuery, usePmConsoleEpisodesQuery } from "../hooks";
import { useLocale } from "../locale/LocaleProvider";

const EPISODE_ARCHIVE_PAGE_SIZE = 6;
const PHASES = [
  ["all", "All episodes"],
  ["open", "Open"],
  ["completed", "Completed"],
] as const;

export function EpisodesPage() {
  const { t } = useLocale();
  const { target = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const phase = searchParams.get("phase") ?? "completed";
  const query = searchParams.get("q") ?? "";
  const requestedEpisodeId = searchParams.get("episode") ?? "";
  const requestedPage = parsePositiveInteger(searchParams.get("page"));
  const requestedOffset = (requestedPage - 1) * EPISODE_ARCHIVE_PAGE_SIZE;
  const listQuery = usePmConsoleEpisodesQuery(target, {
    offset: requestedOffset,
    limit: EPISODE_ARCHIVE_PAGE_SIZE,
    phase,
    query: query || null,
    anchorEpisodeId: requestedEpisodeId || null,
  });
  const episodes = listQuery.data?.episodes ?? [];
  const selectedId = requestedEpisodeId || episodes[0]?.episode_id || "";
  const detailQuery = usePmConsoleEpisodeDetailQuery(target, selectedId);
  const total = listQuery.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / EPISODE_ARCHIVE_PAGE_SIZE));
  const activePage = Math.floor((listQuery.data?.offset ?? requestedOffset) / EPISODE_ARCHIVE_PAGE_SIZE) + 1;
  const pageLabel = `${t("Page")} ${activePage} ${t("of")} ${pageCount}`;
  const visiblePhase = useMemo(() => formatPhase(phase), [phase]);

  if (!target) return <div className="route-empty">{t("No target selected.")}</div>;
  if (listQuery.isLoading) return <div className="route-empty">{t("Loading position episodes...")}</div>;
  if (listQuery.isError) return <div className="route-empty">{t("Position episodes could not be loaded for")} `{target}`.</div>;

  function patchParams(patch: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(patch)) {
      if (!value) next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next);
  }

  function selectEpisode(episodeId: string) {
    patchParams({
      episode: episodeId,
      transition: null,
      page: activePage > 1 ? String(activePage) : null,
    });
  }

  function movePage(nextPage: number) {
    patchParams({
      page: nextPage > 1 ? String(nextPage) : null,
      episode: null,
      transition: null,
    });
  }

  return (
    <div className="page episodes-page">
      <section className="hero-card episodes-hero-card">
        <div className="hero-main">
          <p className="eyebrow">{t("Position lifecycle")}</p>
          <h2>{target.toUpperCase()} {t("Episodes")}</h2>
          <p className="hero-copy">{t("One executed directional view from entry through resizing, exit, and post-close learning.")}</p>
        </div>
        <div className="hero-status-panel">
          <div className="hero-statuses episodes-hero-statuses">
            <MetricBadge label={t("Visible")} value={String(total)} />
            <MetricBadge label={t("Archive")} value={pageLabel} />
            <MetricBadge label={t("Filter")} value={visiblePhase} />
          </div>
        </div>
      </section>

      <section className="episodes-layout" aria-label={t("Position Episodes workspace")}>
        <aside className="episodes-archive-panel">
          <div className="episodes-panel-header">
            <div><p className="eyebrow">{t("Position archive")}</p><h3>{t("Episodes")}</h3></div>
            <span className="timeline-count">{total}</span>
          </div>
          <div className="episodes-filters">
            <div className="episodes-filter-control"><ListFilter size={15} /><label className="sr-only" htmlFor="episode-phase">{t("Episode phase")}</label><select id="episode-phase" value={phase} onChange={(event) => patchParams({ phase: event.target.value, q: null, page: null, episode: null, transition: null })}>{PHASES.map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></div>
            <div className="episodes-filter-control"><Search size={15} /><label className="sr-only" htmlFor="episode-search">{t("Find position episode")}</label><input id="episode-search" value={query} placeholder={t("Direction, close reason, ID")} onChange={(event) => patchParams({ q: event.target.value, page: null, episode: null, transition: null })} /></div>
          </div>
          <div className="episodes-archive-meta"><span>{pageLabel}</span><span>{EPISODE_ARCHIVE_PAGE_SIZE} {t("per page")}</span></div>
          {episodes.length ? <div className="episodes-queue-list">{episodes.map((episode) => <EpisodeQueueItem key={episode.episode_id} episode={episode} selected={episode.episode_id === selectedId} onSelect={() => selectEpisode(episode.episode_id)} />)}</div> : <p className="episodes-empty">{t("No persisted position episodes exist in this workspace snapshot.")}</p>}
          <div className="episodes-pagination">
            <button type="button" className="thesis-mini-button" onClick={() => movePage(activePage - 1)} disabled={activePage <= 1}><ChevronLeft size={15} /> {t("Previous")}</button>
            <span>{pageLabel}</span>
            <button type="button" className="thesis-mini-button" onClick={() => movePage(activePage + 1)} disabled={activePage >= pageCount}><ChevronRight size={15} /> {t("Next")}</button>
          </div>
        </aside>

        {!selectedId ? <EmptyDetail /> : detailQuery.isLoading ? <div className="route-empty">{t("Loading the selected position episode...")}</div> : detailQuery.isError || !detailQuery.data ? <div className="route-empty">{t("The selected position episode is unavailable in this workspace snapshot.")}</div> : <EpisodeWorkspace target={target} detail={detailQuery.data.episode} requestedTransitionId={searchParams.get("transition") ?? ""} onSelectTransition={(transitionId) => patchParams({ transition: transitionId })} />}
      </section>
    </div>
  );
}

function EpisodeQueueItem({ episode, selected, onSelect }: { episode: PMConsoleEpisodeSummary; selected: boolean; onSelect: () => void }) {
  return <button type="button" className={selected ? "episode-queue-item selected" : "episode-queue-item"} onClick={onSelect}>
    <span className="episode-queue-top"><strong>{episode.direction.toUpperCase()} · {formatPhase(episode.phase)}</strong><span className={`episode-status ${statusTone(episode.outcome_status.code)}`}>{episode.strategy_return === null ? formatState(episode.outcome_status.code) : formatReturn(episode.strategy_return)}</span></span>
    <span className="episode-queue-copy">{formatTimestamp(episode.opened_at)}{episode.closed_at ? ` -> ${formatTimestamp(episode.closed_at)}` : " -> active"}</span>
    <span className="episode-queue-meta">{formatSegmentCount(episode.segment_count)} · {formatState(episode.close_reason ?? "open_position")}</span>
  </button>;
}

function EpisodeWorkspace({ target, detail, requestedTransitionId, onSelectTransition }: { target: string; detail: PMConsoleEpisodeDetail; requestedTransitionId: string; onSelectTransition: (transitionId: string) => void }) {
  const { t } = useLocale();
  const { summary } = detail;
  const selectedTransition = selectTransition(detail, requestedTransitionId);
  return <>
    <main className="episodes-detail-column">
      <section className="episodes-thread-header">
        <div><p className="eyebrow">{summary.phase === "open" ? t("Active position") : t("Completed position")}</p><h3>{summary.direction.toUpperCase()} · {formatTimestamp(summary.opened_at)}</h3><p>{summary.closed_at ? `${t("Closed")} ${formatTimestamp(summary.closed_at)} ${t("by")} ${formatState(summary.close_reason ?? "persisted_transition")}.` : t("This executed directional view remains open; current exposure is owned by Position Management.")}</p></div>
        <span className={`episode-status ${statusTone(summary.outcome_status.code)}`}>{summary.strategy_return === null ? formatState(summary.outcome_status.code) : formatReturn(summary.strategy_return)}</span>
      </section>
      <section className="episodes-thread-section">
        <ThreadHeader step="01" title={t("Lifecycle outcome")} status={summary.phase === "open" ? t("Current position") : t("Completed position")} />
        <div className="episodes-fact-grid">
          <Fact label={t("Episode return")} value={summary.strategy_return === null ? t("Not available") : formatReturn(summary.strategy_return)} note={summary.outcome_status.explanation} />
          <Fact label={t("Position path")} value={formatSegmentCount(detail.segments.length)} note={detail.segments.map((segment) => formatWeight(segment.target_weight)).join(" -> ") || t("No persisted segments")} />
          <Fact label={t("Close reason")} value={summary.close_reason ? formatState(summary.close_reason) : t("Still open")} note={t("A basis handover closes this episode and starts a new directional lifecycle.")} />
          <Fact label={t("Reflection")} value={detail.reflection.learning_recorded ? t("Recorded") : formatState(detail.reflection.status.code)} note={detail.reflection.status.explanation} />
        </div>
      </section>
      <section className="episodes-thread-section">
        <ThreadHeader step="02" title={t("Execution price trail")} status={detail.transitions.length ? t("Select one to read") : t("No closed transition snapshot")} />
        {detail.transitions.length ? <div className="episode-price-trail">{detail.transitions.map((transition) => <EpisodePriceNode key={transition.state_change_id} transition={transition} selected={transition.state_change_id === selectedTransition?.state_change_id} onSelect={() => onSelectTransition(transition.state_change_id)} />)}</div> : <p className="episodes-empty">{t("The open artifact confirms current position segments, but has no persisted closed lifecycle snapshot to replay here.")}</p>}
      </section>
    </main>
    <EpisodeTransitionReader target={target} transition={selectedTransition} />
  </>;
}

function EpisodePriceNode({ transition, selected, onSelect }: { transition: PMConsoleEpisodeTransition; selected: boolean; onSelect: () => void }) {
  const { t } = useLocale();
  const price = formatExecutionPrice(transition.adjusted_price);
  return <button type="button" aria-pressed={selected} className={selected ? "episode-price-node selected" : "episode-price-node"} onClick={onSelect}>
    <span className="episode-price-node-top"><strong>{formatState(transition.kind)}</strong><time>{formatTimestamp(transition.occurred_at)}</time></span>
    <span className="episode-price-value"><small>{t("Adjusted execution price")}</small><strong>{price}</strong></span>
    <span className="episode-price-node-meta">{formatWeightTransition(transition)} · {formatState(transition.state)}</span>
  </button>;
}

function EpisodeTransitionReader({ target, transition }: { target: string; transition: PMConsoleEpisodeTransition | null }) {
  const { t } = useLocale();
  if (!transition) return <aside className="episodes-transition-reader episodes-detail-empty"><p className="eyebrow">{t("Selected PM transition")}</p><h3>{t("No transition selected")}</h3><p>{t("There is no persisted transition rationale for this position lifecycle.")}</p></aside>;
  const positionHref = transition.execution_record_id ? `/targets/${encodeURIComponent(target)}/position?marker=pm:${encodeURIComponent(transition.execution_record_id)}` : null;
  const pmHref = transition.pm_decision_id ? `/targets/${encodeURIComponent(target)}/pm?decision=${encodeURIComponent(transition.pm_decision_id)}` : null;
  return <aside className="episodes-transition-reader">
    <div className="episodes-reader-header"><div><p className="eyebrow">{t("Selected PM transition")}</p><h3>{formatState(transition.kind)} {t("rationale")}</h3><p>{formatTimestamp(transition.occurred_at)} · {formatWeightTransition(transition)} · {formatState(transition.state)}</p></div><span className={`episode-status ${transition.kind === "exit" ? "attention" : "ready"}`}>{formatState(transition.kind)}</span></div>
    <div className="episodes-reader-price"><span>{t("Adjusted execution price")}</span><strong>{formatExecutionPrice(transition.adjusted_price)}</strong></div>
    <p className="episodes-reader-note">{t("Full persisted rationale. Select a price node to inspect its exact action and linked records.")}</p>
    <MarkdownContent content={transition.rationale_md || t("No persisted PM rationale is available for this transition.")} className="episodes-rationale-markdown" />
    <div className="episodes-reader-actions">{positionHref ? <Link to={positionHref}><ExternalLink size={14} /> {t("Execution marker")}</Link> : null}{pmHref ? <Link to={pmHref}><ExternalLink size={14} /> {t("PM Trajectory")}</Link> : null}</div>
    <details className="audit-details"><summary>{t("Execution audit")}</summary><div className="audit-details-body"><Fact label={t("Raw execution price")} value={formatExecutionPrice(transition.raw_price)} note={t("Persisted pre-cost market price.")} /><Fact label={t("Execution cost")} value={formatCostBps(transition.total_cost_bps)} note={t("Applied to derive the adjusted execution price.")} /><Fact label={t("Price basis")} value={transition.price_basis ? formatState(transition.price_basis) : t("Not recorded")} note={t("Persisted execution pricing basis.")} /><Fact label={t("State change")} value={transition.state_change_id} note={t("Persisted position-lifecycle transition.")} /><Fact label={t("PM decision")} value={transition.pm_decision_id ?? t("None")} note={t("Linked PM decision record.")} /><Fact label={t("Execution")} value={transition.execution_record_id ?? t("None")} note={t("Linked execution record.")} /></div></details>
  </aside>;
}

function EmptyDetail() { const { t } = useLocale(); return <section className="episodes-detail-empty"><p className="eyebrow">{t("Position archive")}</p><h3>{t("No matching episode")}</h3><p>{t("This workspace has no persisted executed position lifecycle for the selected filter.")}</p></section>; }
function ThreadHeader({ step, title, status }: { step: string; title: string; status: string }) { return <div className="episodes-thread-section-header"><span>{step}</span><h4>{title}</h4><small>{status}</small></div>; }
function Fact({ label, value, note }: { label: string; value: string; note: string }) { return <article className="episodes-fact"><span>{label}</span><strong>{value}</strong><p>{note}</p></article>; }
function MetricBadge({ label, value }: { label: string; value: string }) { return <div className="metric-badge"><span>{label}</span><strong>{value}</strong></div>; }
function selectTransition(detail: PMConsoleEpisodeDetail, requestedId: string) { return detail.transitions.find((item) => item.state_change_id === requestedId) ?? (detail.summary.phase === "completed" ? detail.transitions.find((item) => item.kind === "exit") ?? detail.transitions.at(-1) : detail.transitions.at(-1)) ?? null; }
function parsePositiveInteger(value: string | null) { const parsed = Number.parseInt(value ?? "1", 10); return Number.isFinite(parsed) && parsed >= 1 ? parsed : 1; }
function formatWeight(value: number) { return `${(value * 100).toFixed(0)}%`; }
function formatWeightTransition(transition: PMConsoleEpisodeTransition) { return transition.previous_target_weight === null ? `Target ${formatWeight(transition.target_weight)}` : `${formatWeight(transition.previous_target_weight)} -> ${formatWeight(transition.target_weight)}`; }
function formatExecutionPrice(value: number | null) { return value === null ? "Not recorded" : new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(value); }
function formatCostBps(value: number | null) { return value === null ? "Not recorded" : `${value.toFixed(2)} bps`; }
function formatSegmentCount(value: number) { return `${value} ${value === 1 ? "segment" : "segments"}`; }
function formatReturn(value: number) { return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`; }
function formatTimestamp(value: string) { return new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short", hourCycle: "h23" }).format(new Date(value)); }
function formatPhase(value: string) { return value === "all" ? "All" : formatState(value); }
function formatState(value: string) { return value.split("_").map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(" "); }
function statusTone(value: string) { return value === "available" || value === "recorded" ? "ready" : value.includes("missing") || value === "pending" ? "attention" : "neutral"; }
