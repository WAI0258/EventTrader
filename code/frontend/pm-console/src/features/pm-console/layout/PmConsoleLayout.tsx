import { Outlet } from "react-router-dom";
import type { PMConsoleContextResponse } from "../../../types";
import { ContextStrip } from "../context/ContextStrip";
import { LocaleProvider } from "../locale/LocaleProvider";
import { ThemeProvider, useTheme } from "../theme/ThemeProvider";
import { TargetSidebar } from "./TargetSidebar";
import { usePmConsoleLayoutModel } from "./usePmConsoleLayoutModel";

export interface PmConsoleOutletContext {
  contextQuery: {
    isLoading: boolean;
    isError: boolean;
    data: PMConsoleContextResponse | undefined;
  };
}

export function PmConsoleLayout() {
  return (
    <ThemeProvider>
      <LocaleProvider>
        <PmConsoleLayoutContent />
      </LocaleProvider>
    </ThemeProvider>
  );
}

function PmConsoleLayoutContent() {
  const model = usePmConsoleLayoutModel();
  const { theme } = useTheme();
  const outletContext: PmConsoleOutletContext = {
    contextQuery: model.contextQuery,
  };

  return (
    <div className="app-shell" data-theme={theme}>
      <TargetSidebar
        activeTarget={model.activeTarget}
        activeView={model.activeView}
        targetsQuery={model.targetsQuery}
      />
      <main className="main-panel">
        <ContextStrip
          contextQuery={model.contextQuery}
          overview={model.activeTarget === null}
          mountedTargetCount={model.targetsQuery.data?.targets.length ?? 0}
          targetsQuery={model.targetsQuery}
        />
        <Outlet context={outletContext} />
      </main>
    </div>
  );
}
