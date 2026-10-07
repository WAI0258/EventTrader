import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { PMConsoleContextResponse } from "../../../types";
import { ContextStrip } from "../context/ContextStrip";
import { ThemeProvider } from "../theme/ThemeProvider";
import {
  LocaleProvider,
  PM_CONSOLE_LOCALE_STORAGE_KEY,
  useLocale,
} from "./LocaleProvider";

const context = {
  generated_at: "2025-01-02T03:05:00Z",
  workspace_root: ".local/test-workspace",
  runtime_status: "unknown",
  runtime_mode: "live",
  data_source: "workspace_snapshot",
  explanation: "Workspace-backed context.",
} satisfies PMConsoleContextResponse;

function LocaleProbe() {
  const { locale } = useLocale();
  return <output data-testid="locale-value">{locale}</output>;
}

function renderLocale() {
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <LocaleProbe />
        <ContextStrip contextQuery={{ isLoading: false, isError: false, data: context }} />
      </LocaleProvider>
    </ThemeProvider>,
  );
}

describe("PM Console locale", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to English and persists the frontend language choice", () => {
    renderLocale();

    expect(screen.getByTestId("locale-value")).toHaveTextContent("en");
    expect(screen.getByRole("button", { name: /switch interface language to chinese/i })).toHaveTextContent("中文");

    fireEvent.click(screen.getByRole("button", { name: /switch interface language to chinese/i }));

    expect(screen.getByTestId("locale-value")).toHaveTextContent("zh-CN");
    expect(window.localStorage.getItem(PM_CONSOLE_LOCALE_STORAGE_KEY)).toBe("zh-CN");
    expect(screen.getByText("接口")).toBeInTheDocument();
  });

  it("does not translate runtime values or workspace content", () => {
    renderLocale();
    fireEvent.click(screen.getByRole("button", { name: /switch interface language to chinese/i }));

    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText(".local/test-workspace")).toBeInTheDocument();
  });
});
