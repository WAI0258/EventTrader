import type { PMConsoleStatus } from "../types";

interface StatusPillProps {
  status: PMConsoleStatus;
}

function toneForStatus(code: string): string {
  if (code === "ready") {
    return "status-pill ready";
  }
  if (code.startsWith("partial") || code === "flat_no_executions") {
    return "status-pill partial";
  }
  return "status-pill unavailable";
}

export function StatusPill({ status }: StatusPillProps) {
  return <span className={toneForStatus(status.code)}>{status.code}</span>;
}
