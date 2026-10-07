import { BookOpenText, RefreshCcw } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type {
  PMConsoleTarget,
  PMConsoleTargetView,
  PMConsoleTargetsResponse,
} from "../../../types";
import { useLocale } from "../locale/LocaleProvider";

interface TargetSidebarProps {
  activeTarget: string | null;
  activeView: PMConsoleTargetView | null;
  targetsQuery: {
    isLoading: boolean;
    isError: boolean;
    data: PMConsoleTargetsResponse | undefined;
  };
}

export function TargetSidebar({
  activeTarget,
  activeView,
  targetsQuery,
}: TargetSidebarProps) {
  const navigate = useNavigate();
  const { t } = useLocale();
  const activeTargetRecord =
    targetsQuery.data?.targets.find((target) => target.target_key === activeTarget) ??
    null;

  return (
    <aside className="sidebar">
      <div className="brand-card">
        <h1>{t("PM Console")}</h1>
      </div>
      <div className="sidebar-section">
        <nav className="sidebar-primary-nav" aria-label={t("PM Console")}>
          <a
            className={!activeTarget ? "view-link active" : "view-link"}
            href="/overview"
          >
            {t("PM Overview")}
          </a>
        </nav>
      </div>
      <div className="sidebar-section">
        <div className="sidebar-heading">
          <span>{t("Targets")}</span>
          <RefreshCcw size={14} />
        </div>
        {targetsQuery.isLoading ? (
          <p className="sidebar-empty">{t("Loading target list...")}</p>
        ) : targetsQuery.isError ? (
          <p className="sidebar-empty">{t("Target list unavailable.")}</p>
        ) : targetsQuery.data?.targets.length ? (
          <div className="target-selector-control">
            <select
              id="pm-console-target"
              className="target-selector"
              aria-label={t("Select target")}
              value={activeTarget ?? ""}
              onChange={(event) => {
                const target = targetsQuery.data?.targets.find(
                  (candidate) => candidate.target_key === event.target.value,
                );
                if (!target) return;
                navigate(targetViewPath(target.target_key, resolveTargetView(target, activeView)));
              }}
            >
              <option value="" disabled>
                {t("Select a target")}
              </option>
              {targetsQuery.data.targets.map((target) => (
                <option key={target.target_key} value={target.target_key}>
                  {target.target_key}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <p className="sidebar-empty">{t("No targets discovered in the workspace.")}</p>
        )}
        {targetsQuery.data?.reload_error ? (
          <p className="sidebar-empty">
            {t("Mount catalog reload failed; showing the last valid target set.")} {" "}
            {targetsQuery.data.reload_error}
          </p>
        ) : null}
      </div>
      {activeTarget && activeTargetRecord ? (
        <div className="sidebar-section">
          <div className="sidebar-heading">
            <span>{t("View")}</span>
            <BookOpenText size={14} />
          </div>
          <nav className="view-nav" aria-label={t("Target views")}>
            {activeTargetRecord.implemented_views.includes("operator") ? (
              <a
                className={
                  activeView === "operator" ? "view-link active" : "view-link"
                }
                href={targetViewPath(activeTarget, "operator")}
              >
                {t("Operator")}
              </a>
            ) : null}
            {activeTargetRecord.implemented_views.includes("episodes") ? (
              <a
                className={
                  activeView === "episodes" ? "view-link active" : "view-link"
                }
                href={targetViewPath(activeTarget, "episodes")}
              >
                {t("Episodes")}
              </a>
            ) : null}
            {activeTargetRecord.implemented_views.includes("thesis") ? (
              <a
                className={
                  activeView === "thesis" ? "view-link active" : "view-link"
                }
                href={targetViewPath(activeTarget, "thesis")}
              >
                {t("Thesis Evolution")}
              </a>
            ) : null}
            {activeTargetRecord.implemented_views.includes("pm") ? (
              <a
                className={activeView === "pm" ? "view-link active" : "view-link"}
                href={targetViewPath(activeTarget, "pm")}
              >
                {t("PM Trajectory")}
              </a>
            ) : null}
            {activeTargetRecord.implemented_views.includes("position") ? (
              <a
                className={
                  activeView === "position" ? "view-link active" : "view-link"
                }
                href={targetViewPath(activeTarget, "position")}
              >
                {t("Position Management")}
              </a>
            ) : null}
          </nav>
        </div>
      ) : null}
    </aside>
  );
}

function resolveTargetView(
  target: PMConsoleTarget,
  activeView: PMConsoleTargetView | null
): PMConsoleTargetView {
  if (activeView && target.implemented_views.includes(activeView)) {
    return activeView;
  }
  return target.default_view;
}

function targetViewPath(targetKey: string, view: PMConsoleTargetView) {
  return `/targets/${encodeURIComponent(targetKey)}/${view}`;
}
