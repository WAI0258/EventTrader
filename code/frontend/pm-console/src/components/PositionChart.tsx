import { useEffect, useRef } from "react";
import {
  ColorType,
  LineStyle,
  TickMarkType,
  createChart,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { PMConsoleMarker, PMConsolePoint } from "../types";
import { useTheme, type PMConsoleTheme } from "../features/pm-console/theme/ThemeProvider";

interface PositionChartProps {
  points: PMConsolePoint[];
  markers: PMConsoleMarker[];
  priceDisplayDecimals: number;
  timeZone: ChartTimeZone;
  onMarkerSelect?: (markers: PMConsoleMarker[]) => void;
}

export type ChartTimeZone = "browser" | number;

interface MarkerGroupDetail {
  groupId: string;
  lane: PMConsoleMarker["lane"];
  entries: PMConsoleMarker[];
  representative: PMConsoleMarker;
}

interface MarkerGroupSummary {
  detail: MarkerGroupDetail;
}

function toUnixTimestamp(value: string): UTCTimestamp {
  return Math.floor(new Date(value).getTime() / 1000) as UTCTimestamp;
}

function toDate(value: Time): Date {
  if (typeof value === "number") {
    return new Date(value * 1000);
  }
  if (typeof value === "string") {
    return new Date(value);
  }
  return new Date(value.year, value.month - 1, value.day);
}

function formatInTimeZone(
  value: Time,
  options: Intl.DateTimeFormatOptions,
  timeZone: ChartTimeZone,
  locale?: string,
): string {
  const date = toDate(value);
  if (timeZone === "browser") {
    return new Intl.DateTimeFormat(locale, options).format(date);
  }
  return new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(
    new Date(date.getTime() + timeZone * 60 * 60 * 1000),
  );
}

function chartTickOptions(tickMarkType: TickMarkType): Intl.DateTimeFormatOptions {
  switch (tickMarkType) {
    case TickMarkType.Year:
      return { year: "numeric" };
    case TickMarkType.Month:
      return { month: "short", year: "numeric" };
    case TickMarkType.DayOfMonth:
      return { month: "short", day: "numeric" };
    case TickMarkType.TimeWithSeconds:
      return { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
    default:
      return { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  }
}

function buildSeries(points: PMConsolePoint[], key: keyof PMConsolePoint) {
  return points.flatMap((point) => {
    const value = point[key];
    if (typeof value !== "number") {
      return [];
    }
    return [{ time: toUnixTimestamp(point.time), value }];
  });
}

function formatMarkerState(value: string | null): string {
  if (!value) {
    return "Not available";
  }
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatMarkerWeight(value: number | null): string {
  if (value === null) {
    return "Not available";
  }
  return `${(value * 100).toFixed(0)}%`;
}

function buildPriceFormat(priceDisplayDecimals: number) {
  return {
    type: "price" as const,
    precision: priceDisplayDecimals,
    minMove: 10 ** -priceDisplayDecimals,
  };
}

interface ChartPalette {
  background: string;
  text: string;
  grid: string;
  border: string;
  crosshair: string;
  pm: string;
  buy: string;
  analysis: string;
  tooltipBorder: string;
}

function chartPalette(theme: PMConsoleTheme): ChartPalette {
  return theme === "dark"
    ? {
        background: "#10181d",
        text: "#dbe7e5",
        grid: "rgba(185, 207, 202, 0.12)",
        border: "rgba(185, 207, 202, 0.24)",
        crosshair: "#94bcb4",
        pm: "#41c4b5",
        buy: "#e1a44b",
        analysis: "#94afe7",
        tooltipBorder: "rgba(185, 207, 202, 0.26)",
      }
    : {
        background: "#f7f9f8",
        text: "#24343a",
        grid: "rgba(36, 52, 58, 0.11)",
        border: "rgba(36, 52, 58, 0.2)",
        crosshair: "#58758f",
        pm: "#087f78",
        buy: "#b86b00",
        analysis: "#536f9f",
        tooltipBorder: "rgba(36, 52, 58, 0.16)",
      };
}

function buildDecisionMarkers(markers: PMConsoleMarker[], palette: ChartPalette) {
  const markerDetails = new Map<string, MarkerGroupDetail>();
  const markerDetailsByTime = new Map<string, MarkerGroupDetail[]>();
  const markerGroups = new Map<string, MarkerGroupDetail>();
  for (const marker of markers) {
    const groupId = `${marker.lane}:${marker.time}`;
    const existingGroup = markerGroups.get(groupId);
    if (existingGroup) {
      existingGroup.entries.push(marker);
    } else {
      markerGroups.set(groupId, {
        groupId,
        lane: marker.lane,
        entries: [marker],
        representative: marker,
      });
    }
  }
  const groupedMarkers: MarkerGroupSummary[] = [];
  for (const [groupId, group] of markerGroups.entries()) {
    group.entries.sort((left, right) =>
      left.time.localeCompare(right.time) ||
      left.business_at.localeCompare(right.business_at) ||
      left.marker_id.localeCompare(right.marker_id)
    );
    const representative = group.entries[group.entries.length - 1] ?? group.entries[0];
    group.representative = representative;
    groupedMarkers.push({ detail: group });
    markerDetails.set(groupId, group);
    const timeKey = String(toUnixTimestamp(representative.time));
    const existingGroups = markerDetailsByTime.get(timeKey);
    if (existingGroups) {
      existingGroups.push(group);
    } else {
      markerDetailsByTime.set(timeKey, [group]);
    }
  }

  const pmMarkers = groupedMarkers
    .filter((group) => group.detail.lane === "pm")
    .map(({ detail }) => ({
      id: detail.groupId,
      time: toUnixTimestamp(detail.representative.time),
      position: detail.representative.position,
      shape: detail.representative.shape,
      color: palette.pm,
    }));
  const analysisMarkers = groupedMarkers
    .filter((group) => group.detail.lane === "analysis")
    .map(({ detail }) => ({
      id: detail.groupId,
      time: toUnixTimestamp(detail.representative.time),
      position: detail.representative.position,
      shape: detail.representative.shape,
      color: palette.analysis,
    }));
  return { pmMarkers, analysisMarkers, markerDetails, markerDetailsByTime };
}

function chooseTimeMatchedDetail(
  {
    pointY,
    time,
    markerDetailsByTime,
    priceToCoordinateByLane,
  }: {
    pointY: number;
    time: MouseEventParams["time"];
    markerDetailsByTime: Map<string, MarkerGroupDetail[]>;
    priceToCoordinateByLane: Record<PMConsoleMarker["lane"], (price: number) => number | null>;
  },
): MarkerGroupDetail | null {
  if (time === undefined) {
    return null;
  }
  const candidates = markerDetailsByTime.get(String(time));
  if (!candidates || candidates.length === 0) {
    return null;
  }
  let bestDetail: MarkerGroupDetail | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of candidates) {
    if (candidate.representative.line_value === null) {
      continue;
    }
    const coordinate = priceToCoordinateByLane[candidate.lane](candidate.representative.line_value);
    if (coordinate === null) {
      continue;
    }
    const distance = Math.abs(coordinate - pointY);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestDetail = candidate;
    }
  }
  if (bestDetail === null) {
    return candidates.length === 1 ? candidates[0] ?? null : null;
  }
  return bestDistance <= 24 ? bestDetail : null;
}

function renderTooltip(
  detail: MarkerGroupDetail,
  priceDisplayDecimals: number,
  timeZone: ChartTimeZone,
): string {
  const accentClass = detail.lane === "pm" ? "pm" : "analysis";
  const firstEntry = detail.entries[0];
  const badgeLabel = detail.entries.length > 1 ? String(detail.entries.length) : firstEntry.text;
  const title =
    detail.entries.length > 1
      ? detail.lane === "pm"
        ? "PM decision shifts"
        : "Analysis Direct mappings"
      : firstEntry.label;
  const entriesMarkup = detail.entries
    .map((entry) => {
      const timeLabel = formatInTimeZone(
        toUnixTimestamp(entry.business_at),
        { dateStyle: "medium", timeStyle: "short", hourCycle: "h23" },
        timeZone,
        "en-US",
      );
      return `
        <div class="chart-tooltip-entry">
          <div class="chart-tooltip-meta">
            <span>${formatMarkerState(entry.state)}</span>
            <span>${formatMarkerWeight(entry.target_weight)}</span>
            ${entry.price === null ? "" : `<span>@ ${entry.price.toFixed(priceDisplayDecimals)}</span>`}
          </div>
          <div class="chart-tooltip-meta subtle">
            <span>${timeLabel}</span>
          </div>
        </div>
      `;
    })
    .join("");

  return `
    <div class="chart-tooltip-card ${accentClass}">
      <div class="chart-tooltip-badge">${badgeLabel}</div>
      <div class="chart-tooltip-body">
        <strong>${title}</strong>
        ${entriesMarkup}
      </div>
    </div>
  `;
}

export function PositionChart({
  points,
  markers,
  priceDisplayDecimals,
  timeZone,
  onMarkerSelect,
}: PositionChartProps) {
  const { theme } = useTheme();
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const chartHostRef = useRef<HTMLDivElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const surface = surfaceRef.current;
    const host = chartHostRef.current;
    const tooltip = tooltipRef.current;
    if (!surface || !host || !tooltip) {
      return undefined;
    }

    const palette = chartPalette(theme);
    const { pmMarkers, analysisMarkers, markerDetails, markerDetailsByTime } =
      buildDecisionMarkers(markers, palette);
    const chart = createChart(host, {
      width: Math.max(host.clientWidth, 1),
      height: 360,
      layout: {
        background: { type: ColorType.Solid, color: palette.background },
        textColor: palette.text,
        fontFamily: '"Segoe UI", "Helvetica Neue", sans-serif',
      },
      grid: {
        vertLines: { color: palette.grid },
        horzLines: { color: palette.grid },
      },
      localization: {
        timeFormatter: (time: Time) =>
          formatInTimeZone(
            time,
            { dateStyle: "medium", timeStyle: "short", hourCycle: "h23" },
            timeZone,
          ),
      },
      timeScale: {
        borderColor: palette.border,
        minBarSpacing: 0.1,
        timeVisible: true,
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType, locale: string) =>
          formatInTimeZone(time, chartTickOptions(tickMarkType), timeZone, locale),
      },
      rightPriceScale: {
        borderColor: palette.border,
      },
      crosshair: {
        vertLine: { color: palette.crosshair, style: LineStyle.Dashed },
        horzLine: { color: palette.crosshair, style: LineStyle.Dashed },
      },
    });
    const priceFormat = buildPriceFormat(priceDisplayDecimals);

    const pmSeries = chart.addLineSeries({
      color: palette.pm,
      lineWidth: 3,
      title: "PM pipeline",
      priceFormat,
    });
    const buyHoldSeries = chart.addLineSeries({
      color: palette.buy,
      lineWidth: 2,
      title: "Buy & Hold",
      priceFormat,
    });
    const analysisDirectSeries = chart.addLineSeries({
      color: palette.analysis,
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      title: "Analysis Direct",
      priceFormat,
    });
    pmSeries.setData(buildSeries(points, "pm_pipeline_value"));
    buyHoldSeries.setData(buildSeries(points, "buy_hold_value"));
    analysisDirectSeries.setData(buildSeries(points, "analysis_direct_value"));
    pmSeries.setMarkers(pmMarkers);
    analysisDirectSeries.setMarkers(analysisMarkers);

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: Math.max(host.clientWidth, 1), height: 360 });
    });
    resizeObserver.observe(host);

    const resolveDetail = (param: MouseEventParams): MarkerGroupDetail | null => {
      if (!param.point) {
        return null;
      }
      return (
        (param.hoveredObjectId === undefined
          ? null
          : markerDetails.get(String(param.hoveredObjectId)) ?? null) ??
        chooseTimeMatchedDetail({
          pointY: param.point.y,
          time: param.time,
          markerDetailsByTime,
          priceToCoordinateByLane: {
            pm: (price) => pmSeries.priceToCoordinate(price),
            analysis: (price) => analysisDirectSeries.priceToCoordinate(price),
          },
        })
      );
    };

    const syncTooltip = (param: MouseEventParams) => {
      if (!param.point) {
        tooltip.style.display = "none";
        return;
      }
      const detail = resolveDetail(param);
      if (!detail) {
        tooltip.style.display = "none";
        return;
      }

      tooltip.innerHTML = renderTooltip(detail, priceDisplayDecimals, timeZone);
      tooltip.style.display = "block";

      const left = Math.min(
        surface.clientWidth - tooltip.offsetWidth - 12,
        param.point.x + 16
      );
      const top = Math.max(12, param.point.y - tooltip.offsetHeight - 18);
      tooltip.style.left = `${Math.max(12, left)}px`;
      tooltip.style.top = `${top}px`;
    };
    chart.subscribeCrosshairMove(syncTooltip);
    const selectMarker = (param: MouseEventParams) => {
      const detail = resolveDetail(param);
      if (detail) {
        onMarkerSelect?.(detail.entries);
      }
    };
    chart.subscribeClick(selectMarker);

    return () => {
      chart.unsubscribeCrosshairMove(syncTooltip);
      chart.unsubscribeClick(selectMarker);
      resizeObserver.disconnect();
      chart.remove();
    };
  }, [markers, points, priceDisplayDecimals, timeZone, onMarkerSelect, theme]);

  if (points.length === 0) {
    return (
      <div className="chart-empty">
        <p>No chart points are available for this target.</p>
      </div>
    );
  }

  return (
    <div className="chart-surface" ref={surfaceRef}>
      <div className="chart-host" ref={chartHostRef} />
      <div className="chart-tooltip" ref={tooltipRef} />
    </div>
  );
}
