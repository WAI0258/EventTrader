import type { PMConsolePositionResponse } from "../../../types";

export function formatTimestamp(value: string | null): string {
  if (!value) {
    return "Not available";
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatWeight(value: number | null): string {
  if (value === null) {
    return "Not available";
  }
  return `${(value * 100).toFixed(0)}%`;
}

export function formatPercent(value: number | null): string {
  if (value === null) {
    return "Not available";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(1)}%`;
}

export function formatStateLabel(value: string | null): string {
  if (!value) {
    return "Not available";
  }
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function calculateReturn(value: number | null, baseValue: number): number | null {
  if (value === null) {
    return null;
  }
  return value / baseValue - 1.0;
}

export function calculateMaxDrawdown(
  values: Array<number | null>,
  baseValue: number
): number | null {
  let peak = baseValue;
  let maxDrawdown = 0.0;
  let hasValue = false;

  for (const value of values) {
    if (value === null) {
      continue;
    }
    hasValue = true;
    if (value > peak) {
      peak = value;
    }
    if (peak <= 0.0) {
      continue;
    }
    const drawdown = value / peak - 1.0;
    if (drawdown < maxDrawdown) {
      maxDrawdown = drawdown;
    }
  }

  return hasValue ? maxDrawdown : null;
}

export function buildPmInterpretation(data: PMConsolePositionResponse): string {
  if (data.latest_pm_decision) {
    const timing = formatTimestamp(data.latest_pm_decision.business_at);
    return `${formatStateLabel(data.latest_pm_decision.requested_state)} at ${formatWeight(
      data.latest_pm_decision.requested_target_weight
    )} on ${timing}. ${
      data.latest_pm_decision.execution_required
        ? "Execution was required for the requested shift."
        : "No new execution was required."
    }`;
  }
  return "No PM decision is available for this workspace snapshot.";
}
