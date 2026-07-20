"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

type Theme = "dark" | "light";

const STORAGE_KEY = "signalbridge_theme";

function getInitialTheme(): Theme {
  if (typeof window === "undefined") {
    return "dark";
  }
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function ThemeToggle({ variant = "solid" }: { variant?: "solid" | "ghost" }) {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const initialTheme = getInitialTheme();
    setTheme(initialTheme);
    document.documentElement.dataset.theme = initialTheme;
    setMounted(true);
  }, []);

  function toggleTheme() {
    const nextTheme: Theme = theme === "dark" ? "light" : "dark";
    setTheme(nextTheme);
    document.documentElement.dataset.theme = nextTheme;
    window.localStorage.setItem(STORAGE_KEY, nextTheme);
  }

  const Icon = mounted && theme === "light" ? Moon : Sun;
  const label = mounted && theme === "light" ? "Use dark mode" : "Use light mode";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className={variant === "ghost" ? "theme-toggle theme-toggle-ghost" : "theme-toggle"}
      aria-label={label}
      title={label}
    >
      <Icon className="h-4 w-4" />
      <span>{mounted && theme === "light" ? "Dark" : "Light"}</span>
    </button>
  );
}
