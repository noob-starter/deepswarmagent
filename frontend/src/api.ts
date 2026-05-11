/** Base URL of the backend (empty = same origin, e.g. Docker nginx). On Vercel set VITE_API_BASE_URL to your Render API origin. */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function apiUrl(path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${p}`;
}

export function isUnreachableBackendError(e: unknown): boolean {
  if (e instanceof TypeError) return true;
  const msg = e instanceof Error ? e.message : String(e);
  return /failed to fetch|networkerror|load failed|network request failed/i.test(msg);
}

export type ResearchSession = {
  id: string;
  query: string;
  status: string;
  total_cost_usd: string;
  agent_invocation_count: number;
  created_at: string;
  updated_at: string;
  final_report?: string | null;
  error_message?: string | null;
  graph_state?: Record<string, unknown> | null;
};

export async function createResearch(query: string): Promise<ResearchSession> {
  const res = await fetch(apiUrl("/api/v1/research"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getResearch(id: string): Promise<ResearchSession> {
  const res = await fetch(apiUrl(`/api/v1/research/${id}`));
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

/** Full persisted audit log (PDF / exports); SSE UI buffer may drop older rows. */
export async function fetchResearchEvents(sessionId: string, limit = 25_000): Promise<StreamEvent[]> {
  const res = await fetch(apiUrl(`/api/v1/research/${sessionId}/events?limit=${limit}`));
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export type StreamEvent = {
  id?: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
};

export function openEventSource(sessionId: string, afterId: number): EventSource {
  return new EventSource(apiUrl(`/api/v1/research/${sessionId}/stream?after_id=${afterId}`));
}
