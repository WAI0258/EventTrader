import { Moon, PlugZap, ServerCog, Sun } from "lucide-react";
import type { PMConsoleContextResponse } from "../../../types";
import {
  contextTone,
  formatDataSource,
  formatRuntimeMode,
  formatWorkspacePath,
  runtimeModeTone,
} from "./presentation";
import { useTheme } from "../theme/ThemeProvider";
import { useLocale } from "../locale/LocaleProvider";

interface ContextStripProps {
  contextQuery: {
    isLoading: boolean;
    isError: boolean;
    data: PMConsoleContextResponse | undefined;
  };
  overview?: boolean;
  mountedTargetCount?: number;
  targetsQuery?: {
    isError: boolean;
  };
}

export function ContextStrip({
  contextQuery,
  overview = false,
  mountedTargetCount = 0,
  targetsQuery,
}: ContextStripProps) {
  const { theme, toggleTheme } = useTheme();
  const { locale, toggleLocale, t } = useLocale();
  const themeLabel = theme === "dark" ? t("Dark mode") : t("Light mode");
  const nextThemeLabel = theme === "dark" ? t("light mode") : t("dark mode");
  const themeToggleLabel =
    locale === "zh-CN"
      ? `当前为${themeLabel}。切换至${nextThemeLabel}`
      : `${themeLabel} active. Switch to ${nextThemeLabel}`;
  const themeControl = (
    <button
      className="theme-toggle"
      type="button"
      onClick={toggleTheme}
      aria-label={themeToggleLabel}
      title={locale === "zh-CN" ? `切换至${nextThemeLabel}` : `Switch to ${nextThemeLabel}`}
      aria-pressed={theme === "light"}
    >
      {theme === "dark" ? <Sun size={15} aria-hidden="true" /> : <Moon size={15} aria-hidden="true" />}
      <span className="sr-only">{themeToggleLabel}</span>
    </button>
  );
  const localeControl = (
    <button
      className="locale-toggle"
      type="button"
      onClick={toggleLocale}
      aria-label={locale === "en" ? "Switch interface language to Chinese" : "切换界面语言至英文"}
      title={locale === "en" ? "切换为中文" : "Switch to English"}
    >
      {locale === "en" ? "中文" : "EN"}
    </button>
  );
  const contextActions = (
    <div className="context-actions">
      {localeControl}
      {themeControl}
    </div>
  );
  if (overview) {
    const unavailable = targetsQuery?.isError ?? false;
    return (
      <section className={`context-strip${unavailable ? " offline" : ""}`}>
        <div className="context-row">
          <div className="context-title">
            {unavailable ? <PlugZap size={16} /> : <ServerCog size={16} />}
            <strong>{t("PM Console")}</strong>
          </div>
          <ContextInlineItem
            label={t("API")}
            value={unavailable ? t("Offline") : t("Online")}
            tone={unavailable ? "bad" : "good"}
          />
          <ContextInlineItem
            label={t("Mounted targets")}
            value={unavailable ? t("Unavailable") : String(mountedTargetCount)}
            tone={unavailable ? "warn" : undefined}
          />
          {contextActions}
        </div>
      </section>
    );
  }
  if (contextQuery.isError) {
    return (
      <section className="context-strip offline">
        <div className="context-row">
          <div className="context-title">
            <PlugZap size={16} />
            <strong>{t("PM Console")}</strong>
          </div>
          <ContextInlineItem label={t("API")} value={t("Offline")} tone="bad" />
          <ContextInlineItem label={t("Mode")} value={t("Offline")} tone="bad" />
          <ContextInlineItem label={t("Workspace")} value={t("Not available")} />
          <ContextInlineItem label={t("Source")} value={t("Unavailable")} tone="warn" />
          {contextActions}
        </div>
      </section>
    );
  }

  if (contextQuery.isLoading || !contextQuery.data) {
    return (
      <section className="context-strip">
        <div className="context-row">
          <div className="context-title">
            <ServerCog size={16} />
            <strong>{t("PM Console")}</strong>
          </div>
          <ContextInlineItem label={t("API")} value={t("Loading")} tone="warn" />
          <ContextInlineItem label={t("Mode")} value={t("Loading")} tone="warn" />
          <ContextInlineItem label={t("Workspace")} value={t("Loading")} />
          <ContextInlineItem label={t("Source")} value={t("Loading")} tone="warn" />
          {contextActions}
        </div>
      </section>
    );
  }

  const context = contextQuery.data;
  return (
    <section className="context-strip">
      <div className="context-row">
        <div className="context-title">
          <ServerCog size={16} />
          <strong>{t("PM Console")}</strong>
        </div>
        <ContextInlineItem label={t("API")} value={t("Online")} tone="good" />
        <ContextInlineItem
          label={t("Mode")}
          value={formatRuntimeMode(context.runtime_mode)}
          tone={runtimeModeTone(context.runtime_mode)}
        />
        <ContextInlineItem
          label={t("Workspace")}
          value={formatWorkspacePath(context.workspace_root)}
        />
        <ContextInlineItem
          label={t("Source")}
          value={formatDataSource(context.data_source)}
          tone={contextTone(context.data_source)}
        />
        {contextActions}
      </div>
    </section>
  );
}

function ContextInlineItem({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "warn" | "bad";
}) {
  return (
    <div className="context-inline-item">
      <span>{label}</span>
      <strong>
        {tone ? <i className={`context-dot ${tone}`} aria-hidden="true" /> : null}
        {value}
      </strong>
    </div>
  );
}
