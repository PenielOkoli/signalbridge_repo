import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        app: "var(--page-bg)",
        "app-soft": "var(--page-bg-soft)",
        panel: "var(--panel-bg)",
        "panel-2": "var(--panel-strong)",
        field: "var(--surface-recessed)",
        wash: "var(--surface-wash)",
        hover: "var(--surface-hover)",
        line: "var(--panel-line)",
        "line-strong": "var(--panel-line-strong)",
        ink: {
          1: "var(--text-1)",
          2: "var(--text-2)",
          3: "var(--text-3)"
        },
        accent: {
          DEFAULT: "rgb(var(--brand-accent-rgb) / <alpha-value>)",
          ink: "var(--brand-accent-ink)"
        },
        warn: "rgb(var(--warn-rgb) / <alpha-value>)",
        buy: "rgb(var(--buy-rgb) / <alpha-value>)",
        sell: "rgb(var(--sell-rgb) / <alpha-value>)",
        danger: "rgb(var(--danger-rgb) / <alpha-value>)"
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "var(--font-sans)", "ui-sans-serif", "sans-serif"],
        mono: ["var(--font-mono)", "SFMono-Regular", "Consolas", "monospace"]
      },
      borderRadius: {
        none: "0",
        sm: "4px",
        DEFAULT: "6px",
        md: "6px",
        lg: "8px",
        xl: "8px",
        "2xl": "8px",
        full: "999px"
      },
      boxShadow: {
        terminal: "0 18px 44px var(--shadow-color)",
        focus: "0 0 0 1px rgb(var(--brand-accent-rgb) / 0.48)"
      }
    }
  },
  plugins: []
};

export default config;