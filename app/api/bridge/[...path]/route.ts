import { NextRequest, NextResponse } from "next/server";

type RouteContext = {
  params: Promise<{ path?: string[] }>;
};

export async function GET(request: NextRequest, context: RouteContext) {
  return proxyBridgeRequest(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyBridgeRequest(request, context);
}

async function proxyBridgeRequest(request: NextRequest, context: RouteContext) {
  // Never accept a NEXT_PUBLIC API URL here. The dashboard token belongs only
  // in Vercel's server-side environment and this proxy is its only path to the
  // FastAPI bridge.
  const apiUrl = normalizeApiUrl(process.env.SIGNALBRIDGE_API_URL ?? "");
  const token = process.env.SIGNALBRIDGE_API_TOKEN ?? process.env.API_BEARER_TOKEN ?? "";

  if (!apiUrl || !token) {
    return NextResponse.json(
      { detail: "SignalBridge backend connection is not configured on the server" },
      { status: 503 }
    );
  }

  const params = await context.params;
  const bridgePath = `/${(params.path ?? []).join("/")}`;
  const search = request.nextUrl.search;
  const body = request.method === "GET" ? undefined : await request.text();

  const response = await fetch(`${apiUrl}${bridgePath}${search}`, {
    method: request.method,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": request.headers.get("content-type") ?? "application/json",
      ...(request.headers.get("cookie") ? { Cookie: request.headers.get("cookie") as string } : {})
    },
    body
  });

  const text = await response.text();
  const headers = new Headers({
    "Content-Type": response.headers.get("content-type") ?? "application/json"
  });
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) {
    headers.set("Set-Cookie", setCookie);
  }

  return new NextResponse(text, {
    status: response.status,
    headers
  });
}

function normalizeApiUrl(apiUrl: string) {
  return apiUrl.trim().replace(/\/$/, "");
}
