import { useState } from "react";
import { PositionChart } from "../../../../components/PositionChart";
import type { ChartTimeZone } from "../../../../components/PositionChart";
import type { PMConsoleMarker, PMConsolePoint } from "../../../../types";
import { useLocale } from "../../locale/LocaleProvider";

interface PositionChartSectionProps {
  marketSymbol: string | null;
  targetKey: string;
  barGranularity: string | null;
  priceDisplayDecimals: number;
  points: PMConsolePoint[];
  markers: PMConsoleMarker[];
  selectedMarkers: PMConsoleMarker[];
  showSelectionHint: boolean;
  onMarkerSelect: (markers: PMConsoleMarker[]) => void;
}

export function PositionChartSection({
  marketSymbol,
  targetKey,
  barGranularity,
  priceDisplayDecimals,
  points,
  markers,
  selectedMarkers,
  showSelectionHint,
  onMarkerSelect,
}: PositionChartSectionProps) {
  const { t } = useLocale();
  const [timeZone, setTimeZone] = useState<ChartTimeZone>("browser");

  return (
    <section className="chart-card position-chart-card">
      <div className="card-header">
        <div>
          <p className="eyebrow">
            {marketSymbol ?? targetKey} • normalized equity • analysis window
          </p>
          <h3>{t("PM Pipeline vs Buy & Hold vs Analysis Direct")}</h3>
        </div>
        <div className="chart-header-controls">
          <div className="chart-chip">{barGranularity ?? t("Current")} {t("view")}</div>
          <label className="chart-timezone-control">
            <span>{t("Time")}</span>
            <select
              aria-label={t("Chart time zone")}
              value={timeZone}
              onChange={(event) =>
                setTimeZone(
                  event.target.value === "browser" ? "browser" : Number(event.target.value),
                )
              }
            >
              <option value="browser">{t("Browser local time")}</option>
              {Array.from({ length: 27 }, (_, index) => index - 12).map((offset) => (
                <option key={offset} value={offset}>
                  UTC{offset >= 0 ? "+" : ""}{offset}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <PositionChart
        points={points}
        markers={markers}
        priceDisplayDecimals={priceDisplayDecimals}
        timeZone={timeZone}
        onMarkerSelect={onMarkerSelect}
      />
      {selectedMarkers.length > 1 ? (
        <div className="marker-selection-list">
          <div className="card-header-inline">
            <div>
              <p className="eyebrow">{t("Same timestamp")}</p>
              <h4>{t("Select a decision marker")}</h4>
            </div>
          </div>
          <div className="marker-selection-buttons">
            {selectedMarkers.map((marker) => (
              <button
                key={marker.marker_id}
                type="button"
                className="marker-selection-button"
                onClick={() => onMarkerSelect([marker])}
                aria-label={`${t("Select")} ${marker.kind === "pm_decision" ? t("PM decision") : t("Analysis")} ${t("marker at")} ${marker.business_at}`}
              >
                <span className="marker-selection-time">{marker.business_at}</span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
      <div className="chart-legend">
        <LegendPill label={t("PM pipeline / PortfolioState")} tone="pm" />
        <LegendPill label={`${t("Buy & Hold")} ${marketSymbol ?? targetKey}`} tone="buy" />
        <LegendPill label={t("Analysis Direct / Assessment mapping")} tone="analysis" />
      </div>
      {showSelectionHint ? (
        <p className="marker-selection-hint">
          {t("Select a PM or Analysis marker to inspect its persisted rationale and linked records.")}
        </p>
      ) : null}
      <p className="chart-note">
        {t("Comparison starts from the first available Analysis assessment. PM markers are executed transitions; AN markers open the persisted Analysis rationale and Thesis anchor.")}
      </p>
    </section>
  );
}

function LegendPill({
  label,
  tone,
}: {
  label: string;
  tone: "pm" | "buy" | "analysis";
}) {
  return (
    <span className={`legend-pill ${tone}`}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}
