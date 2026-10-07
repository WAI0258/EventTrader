import { formatPercent, formatStateLabel, formatTimestamp, formatWeight } from "../presentation";

interface OverviewColumnProps {
  currentExposureExplanation: string;
  currentExposureState: string | null;
  currentExposureWeight: number | null;
  interpretation: string;
  pmReturn: number | null;
  buyHoldReturn: number | null;
  analysisDirectReturn: number | null;
  pmMaxDrawdown: number | null;
}

export function OverviewColumn({
  currentExposureExplanation,
  currentExposureState,
  currentExposureWeight,
  interpretation,
  pmReturn,
  buyHoldReturn,
  analysisDirectReturn,
  pmMaxDrawdown,
}: OverviewColumnProps) {
  return (
    <aside className="side-column">
      <section className="side-panel emphasis-panel">
        <p className="eyebrow">Current Exposure</p>
        <div className="exposure-headline">
          {formatStateLabel(currentExposureState)} {" • "}
          {formatWeight(currentExposureWeight)}
        </div>
        <p className="panel-copy">{currentExposureExplanation}</p>
      </section>
      <section className="side-panel interpretation-panel">
        <p className="eyebrow">PM Interpretation</p>
        <h3 className="panel-title">PM stance</h3>
        <p className="panel-copy">{interpretation}</p>
      </section>
      <section className="metric-tile-grid">
        <MetricTile label="PM pipeline" value={formatPercent(pmReturn)} />
        <MetricTile label="Buy & Hold" value={formatPercent(buyHoldReturn)} />
        <MetricTile label="Analysis Direct" value={formatPercent(analysisDirectReturn)} />
        <MetricTile label="PM max drawdown" value={formatPercent(pmMaxDrawdown)} />
      </section>
    </aside>
  );
}

function MetricTile({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <article className="metric-tile">
      <strong>{value}</strong>
      <span>{label}</span>
    </article>
  );
}
