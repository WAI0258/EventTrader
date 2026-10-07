import type { PMConsoleLine, PMConsolePositionResponse } from "../../../../types";
import { StatusPill } from "../../../../components/StatusPill";
import { formatPercent, formatTimestamp } from "../presentation";
import { useLocale } from "../../locale/LocaleProvider";

interface PositionSupportingStateProps {
  data: PMConsolePositionResponse;
  pmLine: PMConsoleLine | undefined;
  buyHoldLine: PMConsoleLine | undefined;
  analysisDirectLine: PMConsoleLine | undefined;
  pmReturn: number | null;
  buyHoldReturn: number | null;
  analysisDirectReturn: number | null;
}

export function PositionSupportingState({
  data,
  pmLine,
  buyHoldLine,
  analysisDirectLine,
  pmReturn,
  buyHoldReturn,
  analysisDirectReturn,
}: PositionSupportingStateProps) {
  const { t } = useLocale();
  const lines = [
    { line: pmLine, value: pmReturn },
    { line: buyHoldLine, value: buyHoldReturn },
    { line: analysisDirectLine, value: analysisDirectReturn },
  ];
  return (
    <section className="position-supporting-state" aria-label={t("Current state and data conditions")}>
      <div className="supporting-state-header">
        <div>
          <p className="eyebrow">{t("Supporting state")}</p>
          <h3>{t("Current state and data conditions")}</h3>
        </div>
        <span className="muted">{t("Read from the selected workspace snapshot")}</span>
      </div>
      <div className="supporting-state-grid">
        <StateCard
          title={t("PortfolioState")}
          value={formatTimestamp(data.current_portfolio_state.updated_at)}
          copy={t("Current executable exposure persisted by the runtime.")}
          status={data.current_portfolio_state.status}
        />
        <StateCard
          title={t("Market data")}
          value={data.market_data_path ?? t("Path not recorded")}
          copy={data.market_data_status.explanation}
          status={data.market_data_status}
        />
        <StateCard
          title={t("Decision markers")}
          value={`${data.markers.length} ${t("persisted markers")}`}
          copy={t("PM and Analysis markers open their persisted records.")}
        />
      </div>
      <div className="supporting-lines">
        <div className="supporting-lines-heading">
          <p className="eyebrow">{t("Line conditions")}</p>
          <span className="muted">{data.base_value.toFixed(2)} {t("base value")}</span>
        </div>
        <div className="supporting-line-list">
          {lines.map(({ line, value }) => (
            <div className="supporting-line-row" key={line?.key ?? "missing-line"}>
              <span>{line?.label ?? t("Line unavailable")}</span>
              <strong>{formatPercent(value)}</strong>
              {line ? <StatusPill status={line.status} /> : <span className="muted">{t("No persisted line")}</span>}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function StateCard({
  title,
  value,
  copy,
  status,
}: {
  title: string;
  value: string;
  copy: string;
  status?: PMConsolePositionResponse["market_data_status"];
}) {
  return (
    <article className="supporting-state-card">
      <div className="summary-label-row">
        <span className="mini-definition-title">{title}</span>
        {status ? <StatusPill status={status} /> : null}
      </div>
      <strong className="supporting-state-value">{value}</strong>
      <p>{copy}</p>
    </article>
  );
}
