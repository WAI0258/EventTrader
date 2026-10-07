import { lazy, Suspense } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";
import { PmConsoleLayout } from "../features/pm-console/layout/PmConsoleLayout";
import { OverviewPage } from "../features/pm-console/overview/OverviewPage";
import { PositionPage } from "../features/pm-console/position/PositionPage";
import { PMEvolutionPage } from "../features/pm-console/pm/PMEvolutionPage";
import { RootRedirect } from "../features/pm-console/position/RootRedirect";
import { ThesisPage } from "../features/pm-console/thesis/ThesisPage";

const OperatorPage = lazy(() =>
  import("../features/pm-console/operator/OperatorPage").then(({ OperatorPage }) => ({
    default: OperatorPage,
  })),
);

const EpisodesPage = lazy(() =>
  import("../features/pm-console/episodes/EpisodesPage").then(({ EpisodesPage }) => ({
    default: EpisodesPage,
  })),
);

export const appRouter = createBrowserRouter([
  {
    path: "/",
    Component: PmConsoleLayout,
    children: [
      {
        index: true,
        Component: RootRedirect,
      },
      {
        path: "overview",
        Component: OverviewPage,
      },
      {
        path: "targets/:target/position",
        Component: PositionPage,
      },
      {
        path: "targets/:target/operator",
        element: (
          <Suspense fallback={<div className="route-empty">Loading Operator Context...</div>}>
            <OperatorPage />
          </Suspense>
        ),
      },
      {
        path: "targets/:target/episodes",
        element: (
          <Suspense fallback={<div className="route-empty">Loading position episodes...</div>}>
            <EpisodesPage />
          </Suspense>
        ),
      },
      {
        path: "targets/:target/thesis",
        Component: ThesisPage,
      },
      {
        path: "targets/:target/pm",
        Component: PMEvolutionPage,
      },
      {
        path: "*",
        element: <Navigate replace to="/" />,
      },
    ],
  },
]);
