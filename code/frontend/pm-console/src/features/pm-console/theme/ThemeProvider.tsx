import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type PMConsoleTheme = "dark" | "light";

const STORAGE_KEY = "pm-console.theme";

interface ThemeContextValue {
  theme: PMConsoleTheme;
  setTheme: (theme: PMConsoleTheme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredTheme(): PMConsoleTheme {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function persistTheme(theme: PMConsoleTheme) {
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Private browsing and embedded contexts can deny storage. The session still works.
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<PMConsoleTheme>(readStoredTheme);

  const setTheme = (nextTheme: PMConsoleTheme) => {
    setThemeState(nextTheme);
    persistTheme(nextTheme);
  };

  const value = useMemo(
    () => ({
      theme,
      setTheme,
      toggleTheme: () => setTheme(theme === "dark" ? "light" : "dark"),
    }),
    [theme],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    persistTheme(theme);
  }, [theme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  return context ?? {
    theme: "dark",
    setTheme: () => undefined,
    toggleTheme: () => undefined,
  };
}

export { STORAGE_KEY as PM_CONSOLE_THEME_STORAGE_KEY };
