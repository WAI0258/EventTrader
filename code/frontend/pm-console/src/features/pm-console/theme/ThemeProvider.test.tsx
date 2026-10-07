import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { PMConsoleContextResponse } from "../../../types";
import { ContextStrip } from "../context/ContextStrip";
import {
  PM_CONSOLE_THEME_STORAGE_KEY,
  ThemeProvider,
  useTheme,
} from "./ThemeProvider";

const context = {
  generated_at: "2025-01-02T03:05:00Z",
  workspace_root: ".local/test-workspace",
  runtime_status: "unknown",
  runtime_mode: "live",
  data_source: "workspace_snapshot",
  explanation: "Workspace-backed context.",
} satisfies PMConsoleContextResponse;

function ShellProbe() {
  const { theme } = useTheme();
  return (
    <div data-theme={theme}>
      <output data-testid="theme-value">{theme}</output>
      <ContextStrip
        contextQuery={{ isLoading: false, isError: false, data: context }}
      />
    </div>
  );
}

function renderTheme() {
  return render(
    <ThemeProvider>
      <ShellProbe />
    </ThemeProvider>,
  );
}

describe("PM Console theme", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to dark mode", () => {
    renderTheme();

    expect(screen.getByTestId("theme-value")).toHaveTextContent("dark");
    expect(screen.getByRole("button", { name: /dark mode active/i })).toHaveAttribute(
      "aria-label",
      "Dark mode active. Switch to light mode",
    );
  });

  it("toggles with an accessible control and persists the chosen mode", () => {
    renderTheme();

    fireEvent.click(screen.getByRole("button", { name: /switch to light mode/i }));

    expect(screen.getByTestId("theme-value")).toHaveTextContent("light");
    expect(screen.getByRole("button", { name: /light mode active/i })).toHaveAttribute(
      "aria-label",
      "Light mode active. Switch to dark mode",
    );
    expect(window.localStorage.getItem(PM_CONSOLE_THEME_STORAGE_KEY)).toBe("light");
  });

  it("keeps the Overview context strip global and concise", () => {
    render(
      <ThemeProvider>
        <ContextStrip
          contextQuery={{ isLoading: false, isError: false, data: context }}
          overview
          mountedTargetCount={2}
          targetsQuery={{ isError: false }}
        />
      </ThemeProvider>,
    );

    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.queryByText("Target-scoped")).not.toBeInTheDocument();
  });
});
