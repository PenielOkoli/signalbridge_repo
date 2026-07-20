import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function BaseIcon({ children, ...props }: IconProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export function Activity(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M3 12h4l2-6 4 12 2-6h6" />
    </BaseIcon>
  );
}

export function Bot(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <rect x="5" y="8" width="14" height="10" rx="2" />
      <path d="M12 4v4" />
      <path d="M9 12h.01" />
      <path d="M15 12h.01" />
      <path d="M8 18v2" />
      <path d="M16 18v2" />
    </BaseIcon>
  );
}

export function Cable(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M7 6V4H3v4h2" />
      <path d="M17 20v-2h4v-4h-2" />
      <path d="M5 8v7a3 3 0 0 0 6 0V6.5a3 3 0 0 1 6 0V16" />
    </BaseIcon>
  );
}

export function Gauge(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M12 14l4-4" />
      <path d="M4 14a8 8 0 1 1 16 0" />
      <path d="M6 18h12" />
    </BaseIcon>
  );
}

export function KeyRound(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <circle cx="8" cy="15" r="4" />
      <path d="M12 15h9" />
      <path d="M18 12v6" />
      <path d="M21 12v3" />
    </BaseIcon>
  );
}

export function Link2(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M10 13a5 5 0 0 1 0-7l1-1a5 5 0 0 1 7 7l-1 1" />
      <path d="M14 11a5 5 0 0 1 0 7l-1 1a5 5 0 0 1-7-7l1-1" />
    </BaseIcon>
  );
}

export function Lock(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <rect x="4" y="11" width="16" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 1 1 8 0v3" />
    </BaseIcon>
  );
}

export function LogOut(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </BaseIcon>
  );
}

export function Play(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="m8 5 11 7-11 7z" />
    </BaseIcon>
  );
}

export function RadioTower(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M12 4 7 20" />
      <path d="M12 4 17 20" />
      <path d="M5 20h14" />
      <path d="M8.5 12a5 5 0 0 1 7 0" />
      <path d="M6.5 8a8 8 0 0 1 11 0" />
    </BaseIcon>
  );
}

export function RefreshCw(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </BaseIcon>
  );
}

export function Save(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z" />
      <path d="M17 21v-8H7v8" />
      <path d="M7 3v5h8" />
    </BaseIcon>
  );
}

export function Search(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </BaseIcon>
  );
}

export function Settings(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1V21a2 2 0 1 1-4 0v-.08a1.7 1.7 0 0 0-.4-1 1.7 1.7 0 0 0-1-.6 1.7 1.7 0 0 0-1.82.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1-.4H3a2 2 0 1 1 0-4h.08a1.7 1.7 0 0 0 1-.4 1.7 1.7 0 0 0 .6-1 1.7 1.7 0 0 0-.34-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1V3a2 2 0 1 1 4 0v.08a1.7 1.7 0 0 0 .4 1 1.7 1.7 0 0 0 1 .6 1.7 1.7 0 0 0 1.82-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.25.31.46.66.6 1 .14.34.21.7.2 1.08a1.7 1.7 0 0 0 .4 1 1.7 1.7 0 0 0 1 .4H21a2 2 0 1 1 0 4h-.08a1.7 1.7 0 0 0-1 .4 1.7 1.7 0 0 0-.52 1.12Z" />
    </BaseIcon>
  );
}

export function ShieldCheck(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M12 3 5 6v6c0 5 3.5 8 7 9 3.5-1 7-4 7-9V6l-7-3Z" />
      <path d="m9 12 2 2 4-4" />
    </BaseIcon>
  );
}

export function SlidersHorizontal(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M3 6h18" />
      <path d="M3 12h18" />
      <path d="M3 18h18" />
      <circle cx="8" cy="6" r="2" />
      <circle cx="16" cy="12" r="2" />
      <circle cx="11" cy="18" r="2" />
    </BaseIcon>
  );
}

export function Square(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <rect x="5" y="5" width="14" height="14" rx="1" />
    </BaseIcon>
  );
}

export function Terminal(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="m4 17 6-6-6-6" />
      <path d="M12 19h8" />
    </BaseIcon>
  );
}

export function TriangleAlert(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="m12 3 10 18H2L12 3Z" />
      <path d="M12 9v5" />
      <path d="M12 18h.01" />
    </BaseIcon>
  );
}

export function Unlock(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <rect x="4" y="11" width="16" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 7-2" />
    </BaseIcon>
  );
}
