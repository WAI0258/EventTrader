import { useMemo } from "react";
import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { PMConsoleTargetView } from "../../../types";
import { usePmConsoleContextQuery, usePmConsoleTargetsQuery } from "../hooks";

export function usePmConsoleLayoutModel() {
  const location = useLocation();
  const navigate = useNavigate();
  const routeContext = useMemo(() => {
    const match = location.pathname.match(
      /^\/targets\/([^/]+)\/(operator|episodes|thesis|pm|position)$/
    );
    const activeView: PMConsoleTargetView | null = match?.[2]
      ? (match[2] as PMConsoleTargetView)
      : null;
    return {
      activeTarget: match?.[1] ? decodeURIComponent(match[1]) : null,
      activeView,
    };
  }, [location.pathname]);

  const contextQuery = usePmConsoleContextQuery(routeContext.activeTarget);
  const targetsQuery = usePmConsoleTargetsQuery();

  useEffect(() => {
    if (!routeContext.activeTarget || !targetsQuery.data) {
      return;
    }
    const stillMounted = targetsQuery.data.targets.some(
      (target) => target.target_key === routeContext.activeTarget
    );
    if (!stillMounted) {
      navigate("/overview", { replace: true });
    }
  }, [navigate, routeContext.activeTarget, targetsQuery.data]);

  return {
    activeTarget: routeContext.activeTarget,
    activeView: routeContext.activeView,
    contextQuery,
    targetsQuery,
  };
}
