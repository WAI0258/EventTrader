import { StatusPill } from "../../../../components/StatusPill";
import type { PMConsolePositionResponse, PMConsoleStatus } from "../../../../types";
import { formatTimestamp } from "../presentation";
import { useLocale } from "../../locale/LocaleProvider";

interface PositionHeroProps {
  data: PMConsolePositionResponse;
}

export function PositionHero({ data }: PositionHeroProps) {
  const { t } = useLocale();
  return (
    <header className="hero-card position-hero-card">
      <div className="hero-main">
        <div>
          <p className="eyebrow">{t("Target")}</p>
          <h2>{data.target_key}</h2>
        </div>
        <div className="hero-meta-row">
          <span className="hero-meta-pill">{data.market_symbol ?? t("No symbol")}</span>
          <span className="hero-meta-pill">{data.bar_granularity ?? t("No granularity")}</span>
          <span className="hero-meta-pill">{t("Generated")} {formatTimestamp(data.generated_at)}</span>
        </div>
      </div>
      <div className="hero-status-panel">
        <div className="hero-statuses">
          <MetricBadge label={t("Market Data")} status={data.market_data_status} />
          <MetricBadge
            label={t("Portfolio State")}
            status={data.current_portfolio_state.status}
          />
        </div>
      </div>
    </header>
  );
}

function MetricBadge({
  label,
  status,
}: {
  label: string;
  status: PMConsoleStatus;
}) {
  return (
    <div className="metric-badge">
      <span>{label}</span>
      <StatusPill status={status} />
    </div>
  );
}
