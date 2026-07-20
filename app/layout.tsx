import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "SignalBridge",
  description: "AI trading signal copier for Telegram channels and major crypto futures exchanges"
};

export default function RootLayout({ children }: { children: ReactNode }) {
  const themeScript = `
    (() => {
      try {
        const stored = window.localStorage.getItem("signalbridge_theme");
        const system = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
        document.documentElement.dataset.theme = stored || system;
      } catch {
        document.documentElement.dataset.theme = "dark";
      }
    })();
  `;

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
