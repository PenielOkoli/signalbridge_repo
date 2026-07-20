import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        void: "#09100c",
        panel: "#1a211d",
        "panel-2": "#161d19",
        line: "rgba(187,202,191,0.16)",
        gold: {
          signal: "#ffb95f",
          dim: "#472a00"
        },
        fuchsia: {
          signal: "#fbabff",
          dim: "#580065"
        },
        emerald: {
          signal: "#4edea3",
          dim: "#00422b"
        },
        indigo: {
          wire: "#6366f1",
          dim: "#312e81"
        }
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "monospace"]
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
        terminal: "0 18px 44px rgba(0, 0, 0, 0.28)",
        focus: "0 0 0 1px rgba(246, 200, 76, 0.48)"
      }
    }
  },
  plugins: []
};

export default config;
