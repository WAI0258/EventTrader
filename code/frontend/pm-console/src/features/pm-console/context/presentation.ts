import type { PMConsoleContextResponse } from "../../../types";

export function formatRuntimeMode(
  value: PMConsoleContextResponse["runtime_mode"]
): string {
  switch (value) {
    case "live":
      return "Live";
    case "replay":
      return "Replay";
    case "catchup":
      return "Catchup";
    default:
      return "Offline";
  }
}

export function formatDataSource(
  value: PMConsoleContextResponse["data_source"]
): string {
  switch (value) {
    case "workspace_snapshot":
      return "Workspace snapshot";
    case "runtime_updating_workspace":
      return "Runtime updating workspace";
    default:
      return "Unknown";
  }
}

export function formatWorkspacePath(value: string): string {
  const normalized = value.replaceAll("\\", "/");
  const marker = normalized.indexOf("/.local/");
  if (marker >= 0) {
    return normalized.slice(marker + 1);
  }
  return normalized;
}

export function runtimeModeTone(
  value: PMConsoleContextResponse["runtime_mode"]
): "good" | "warn" | "bad" {
  if (value === "live") {
    return "good";
  }
  if (value === "replay" || value === "catchup") {
    return "warn";
  }
  return "bad";
}

export function contextTone(
  value:
    | PMConsoleContextResponse["runtime_mode"]
    | PMConsoleContextResponse["data_source"]
    | "online"
    | "offline"
): "good" | "warn" | "bad" {
  if (value === "online" || value === "live" || value === "workspace_snapshot") {
    return "good";
  }
  if (
    value === "replay" ||
    value === "catchup" ||
    value === "runtime_updating_workspace" ||
    value === "unknown"
  ) {
    return "warn";
  }
  return "bad";
}
