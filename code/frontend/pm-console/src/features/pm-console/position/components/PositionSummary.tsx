import { StatusPill } from "../../../../components/StatusPill";
import type { PMConsoleStatus } from "../../../../types";
import { formatPercent, formatStateLabel, formatWeight } from "../presentation";
import { useLocale } from "../../locale/LocaleProvider";

interface PositionSummaryProps {
  currentExposureExplanation: string;
  currentExposureState: string | null;
  currentExposureWeight: number | null;
  currentExposureStatus: PMConsoleStatus;
  interpretation: string;
  pmReturn: number | null;
  buyHoldReturn: number | null;
  analysisDirectReturn: number | null;
  pmMaxDrawdown: number | null;
}

export function PositionSummary({
  currentExposureExplanation,
  currentExposureState,
  currentExposureWeight,
  currentExposureStatus,
  interpretation,
  pmReturn,
  buyHoldReturn,
  analysisDirectReturn,
  pmMaxDrawdown,
}: PositionSummaryProps) {
  const { t } = useLocale();
  return (
    <section className="position-summary" aria-label={t("Position summary")}>
      <article className="position-exposure-summary">
        <div className="summary-label-row">
          <p className="eyebrow">{t("Current exposure")}</p>
          <StatusPill status={currentExposureStatus} />
        </div>
        <strong className="exposure-headline">
          {formatStateLabel(currentExposureState)} <span>·</span> {formatWeight(currentExposureWeight)}
        </strong>
        <p className="panel-copy">{currentExposureExplanation}</p>
        <p className="position-interpretation">{interpretation}</p>
      </article>
      <MetricTile label={t("PM pipeline")} sublabel={t("Execution path")} value={formatPercent(pmReturn)} tone="actual" />
      <MetricTile label={t("Buy & Hold")} sublabel={t("Baseline")} value={formatPercent(buyHoldReturn)} tone="baseline" />
      <MetricTile label={t("Analysis Direct")} sublabel={t("Assessment mapping")} value={formatPercent(analysisDirectReturn)} tone="analysis" />
      <MetricTile label={t("PM max drawdown")} sublabel={t("Realized path")} value={formatPercent(pmMaxDrawdown)} tone="risk" />
    </section>
  );
}

function MetricTile({
  label,
  sublabel,
  value,
  tone,
}: {
  label: string;
  sublabel: string;
  value: string;
  tone: "actual" | "baseline" | "analysis" | "risk";
}) {
  return (
    <article className={`position-metric-tile ${tone}`}>
      <div className="position-metric-heading">
        <span className="eyebrow">{label}</span>
        <span className="position-metric-sublabel">{sublabel}</span>
      </div>
      <strong>{value}</strong>
    </article>
  );
}
