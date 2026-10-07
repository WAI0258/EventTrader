import type {
  PMConsoleLine,
  PMConsolePositionResponse,
} from "../../../../types";
import { formatPercent, formatTimestamp } from "../presentation";

interface ExposureDetailsColumnProps {
  data: PMConsolePositionResponse;
  pmLine: PMConsoleLine | undefined;
  buyHoldLine: PMConsoleLine | undefined;
  analysisDirectLine: PMConsoleLine | undefined;
  pmReturn: number | null;
  buyHoldReturn: number | null;
  analysisDirectReturn: number | null;
}

export function ExposureDetailsColumn({
  data,
  pmLine,
  buyHoldLine,
  analysisDirectLine,
  pmReturn,
  buyHoldReturn,
  analysisDirectReturn,
}: ExposureDetailsColumnProps) {
  return (
    <aside className="side-column">
      <section className="side-panel compact-stack">
        <MiniDefinitionCard
          title="PortfolioState"
          value={formatTimestamp(data.current_portfolio_state.updated_at)}
          copy="Current executable exposure persisted by the runtime."
        />
        <MiniDefinitionCard
          title="PM pipeline"
          value={pmLine ? formatPercent(pmReturn) : "Not available"}
          copy={
            pmLine
              ? "Realized execution path from `PMDecision` to `PortfolioState`."
              : "PM pipeline is unavailable."
          }
        />
        <MiniDefinitionCard
          title="Buy & Hold"
          value={buyHoldLine ? formatPercent(buyHoldReturn) : "Not available"}
          copy={
            buyHoldLine?.status.code === "ready"
              ? `${data.market_symbol ?? data.target_key} held across the analysis window.`
              : (buyHoldLine?.status.explanation ??
              "Buy & Hold is unavailable.")
          }
        />
        <MiniDefinitionCard
          title="Analysis Direct"
          value={analysisDirectLine ? formatPercent(analysisDirectReturn) : "Not available"}
          copy={
            analysisDirectLine
              ? "Deterministic AnalysisAssessment state mapping; select an AN marker for its Thesis-anchored rationale."
              : "Analysis Direct is unavailable."
          }
        />
      </section>
    </aside>
  );
}

function MiniDefinitionCard({
  title,
  value,
  copy,
}: {
  title: string;
  value: string;
  copy: string;
}) {
  return (
    <article className="mini-definition-card">
      <span className="mini-definition-title">{title}</span>
      <strong className="mini-definition-value">{value}</strong>
      <p className="mini-definition-copy">{copy}</p>
    </article>
  );
}
