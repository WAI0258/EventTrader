import type { PMConsoleLine, PMConsolePositionResponse } from "../../../types";
import { usePmConsolePositionQuery } from "../hooks";
import {
  buildPmInterpretation,
  calculateMaxDrawdown,
  calculateReturn,
} from "./presentation";

interface PositionPageReadyModel {
  state: "ready";
  data: PMConsolePositionResponse;
  pmLine: PMConsoleLine | undefined;
  buyHoldLine: PMConsoleLine | undefined;
  analysisDirectLine: PMConsoleLine | undefined;
  pmReturn: number | null;
  buyHoldReturn: number | null;
  analysisDirectReturn: number | null;
  pmMaxDrawdown: number | null;
  interpretation: string;
}

interface PositionPagePendingModel {
  state: "missing_target" | "loading" | "error";
}

export function usePositionPageModel(
  targetKey: string
): PositionPageReadyModel | PositionPagePendingModel {
  const positionQuery = usePmConsolePositionQuery(targetKey);

  if (!targetKey) {
    return { state: "missing_target" };
  }

  if (positionQuery.isLoading) {
    return { state: "loading" };
  }

  if (positionQuery.isError || !positionQuery.data) {
    return { state: "error" };
  }

  const data = positionQuery.data;
  const latestPoint = data.points.length === 0 ? null : data.points[data.points.length - 1];
  const lineByKey = new Map(data.lines.map((line) => [line.key, line]));
  const pmLine = lineByKey.get("pm_pipeline");
  const buyHoldLine = lineByKey.get("buy_hold");
  const analysisDirectLine = lineByKey.get("analysis_direct");
  const pmReturn = calculateReturn(latestPoint?.pm_pipeline_value ?? null, data.base_value);
  const buyHoldReturn = calculateReturn(latestPoint?.buy_hold_value ?? null, data.base_value);
  const analysisDirectReturn = calculateReturn(
    latestPoint?.analysis_direct_value ?? null,
    data.base_value
  );
  const pmMaxDrawdown = calculateMaxDrawdown(
    data.points.map((point) => point.pm_pipeline_value),
    data.base_value
  );

  return {
    state: "ready",
    data,
    pmLine,
    buyHoldLine,
    analysisDirectLine,
    pmReturn,
    buyHoldReturn,
    analysisDirectReturn,
    pmMaxDrawdown,
    interpretation: buildPmInterpretation(data),
  };
}
