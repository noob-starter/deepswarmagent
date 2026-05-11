import {
  Background,
  ControlButton,
  Controls,
  type Edge,
  Handle,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { downloadResearchPdf, mergeEventsKeepingClaimStream, searchWaveSubtasksFromEvents, streamEventsFromGraphVerifiedClaims } from "./buildResearchPdf";
import {
  createResearch,
  getResearch,
  isUnreachableBackendError,
  openEventSource,
  type StreamEvent,
} from "./api";

/** Survives F5 in this tab; cleared when the tab/window closes (browser clears sessionStorage) or on Clear session. */
const SESSION_STORAGE_KEY = "deep_research_swarm_session_id";

const SERVER_UNREACHABLE_MESSAGE = "Server is unreachable at the moment.";

function shortSessionLabel(id: string | null): string {
  if (!id) return "—";
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}…`;
}

const AGENT_TREE: { id: string; label: string; parent: string | null }[] = [
  { id: "orchestrator", label: "Orchestrator", parent: null },
  { id: "planner", label: "Planner", parent: "orchestrator" },
  { id: "parallel_search", label: "Search wave", parent: "orchestrator" },
  { id: "critic", label: "Critic", parent: "orchestrator" },
  { id: "critic_route_prepare", label: "Critic router", parent: "orchestrator" },
  { id: "extract_claims", label: "Claim extraction", parent: "orchestrator" },
  { id: "fact_check", label: "Fact check", parent: "orchestrator" },
  { id: "synthesize", label: "Synthesizer", parent: "orchestrator" },
  { id: "citation_format", label: "Citation formatter", parent: "orchestrator" },
];

const EXPANDABLE_PARENT_IDS = new Set(["parallel_search", "fact_check"]);

function isExpandableParent(id: string): boolean {
  return EXPANDABLE_PARENT_IDS.has(id);
}

function statusBorderColor(st: "idle" | "running" | "done" | "failed"): string {
  return st === "running"
    ? "#fbbf24"
    : st === "done"
      ? "#34d399"
      : st === "failed"
        ? "#f87171"
        : "#64748b";
}

function sortEventsChronologically(events: StreamEvent[]): StreamEvent[] {
  return [...events]
    .map((e, idx) => ({ e, idx }))
    .sort((a, b) => (a.e.id ?? a.idx) - (b.e.id ?? b.idx))
    .map(({ e }) => e);
}

type SubAgentSpec = { nodeId: string; title: string; detail: string };

function collectParallelSearchSubs(events: StreamEvent[]): SubAgentSpec[] {
  return searchWaveSubtasksFromEvents(events).map((r) => ({
    nodeId: `sub:parallel_search:${r.agentId}`,
    title: (r.question || r.agentId).slice(0, 80) || r.agentId,
    detail: r.railsAndHits,
  }));
}

function collectFactCheckSubs(events: StreamEvent[]): SubAgentSpec[] {
  const sorted = sortEventsChronologically(events);
  const lastByClaim = new Map<string, StreamEvent>();
  for (const e of sorted) {
    if (e.event_type !== "claim_verified") continue;
    const p = e.payload as { claim_id?: string };
    const cid = String(p.claim_id || "");
    if (!cid) continue;
    lastByClaim.set(cid, e);
  }
  return [...lastByClaim.entries()].map(([cid, e]) => {
    const p = e.payload as { trust_score?: number; claim?: string };
    const t = p.trust_score ?? 0;
    return {
      nodeId: `sub:fact_check:${cid}`,
      title: `Trust ${t}/100`,
      detail: (p.claim || cid).slice(0, 96),
    };
  });
}

/** Approximate main agent card size (grid layout) for positioning sub nodes */
const PARENT_CARD_W = 132;
const PARENT_CARD_H = 58;
/** Space between stacked child nodes */
const SUB_SIBLING_GAP = 30;
/** Extra space from parent card edge to first child (larger = farther from parent) */
const PARENT_TO_CHILD_GAP = 72;
const SUB_CARD_EST_H = 78;
const SUB_CARD_W = 160;

/** Search wave: vertical column to the right of the parent */
function placeParallelSearchSubs(px: number, py: number, childNodes: Node[]) {
  const startX = px + PARENT_CARD_W + PARENT_TO_CHILD_GAP;
  let y = py;
  for (const cn of childNodes) {
    cn.position = { x: startX, y };
    y += SUB_CARD_EST_H + SUB_SIBLING_GAP;
  }
}

/** Fact check: horizontal row below the parent, centered */
function placeFactCheckSubs(px: number, py: number, childNodes: Node[]) {
  const startY = py + PARENT_CARD_H + PARENT_TO_CHILD_GAP;
  const n = childNodes.length;
  if (n === 0) return;
  const totalW = n * SUB_CARD_W + (n - 1) * SUB_SIBLING_GAP;
  const parentCenterX = px + PARENT_CARD_W / 2;
  let x = parentCenterX - totalW / 2;
  for (const cn of childNodes) {
    cn.position = { x: Math.max(8, x), y: startY };
    x += SUB_CARD_W + SUB_SIBLING_GAP;
  }
}

function layoutNodes(statusByAgent: Record<string, "idle" | "running" | "done" | "failed">): {
  nodes: Node[];
  edges: Edge[];
} {
  const nodes: Node[] = AGENT_TREE.map((a, i) => {
    const row = Math.floor(i / 3);
    const col = i % 3;
    const st = statusByAgent[a.id] ?? "idle";
    const color = statusBorderColor(st);
    return {
      id: a.id,
      position: { x: col * 200, y: row * 110 },
      data: {
        label: a.label,
        status: st,
      },
      style: {
        background: "#0f172a",
        border: `2px solid ${color}`,
        borderRadius: 8,
        padding: "10px 14px",
        color: "#f1f5f9",
        fontSize: 13,
        minWidth: 120,
      },
    };
  });
  const edges: Edge[] = AGENT_TREE.filter((a) => a.parent).map((a) => ({
    id: `${a.parent}-${a.id}`,
    source: a.parent!,
    target: a.id,
    animated: statusByAgent[a.id] === "running",
    style: { stroke: "#475569", strokeWidth: 1.5 },
  }));
  return { nodes, edges };
}

function buildAgentGraph(
  statusByAgent: Record<string, "idle" | "running" | "done" | "failed">,
  expandedParentId: string | null,
  events: StreamEvent[],
  selectedAgentId: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const { nodes: baseNodes, edges: baseEdges } = layoutNodes(statusByAgent);

  if (!expandedParentId || !isExpandableParent(expandedParentId)) {
    const withSelection = baseNodes.map((n) => ({ ...n, selected: selectedAgentId === n.id }));
    return { nodes: withSelection, edges: baseEdges };
  }

  const subs =
    expandedParentId === "parallel_search"
      ? collectParallelSearchSubs(events)
      : expandedParentId === "fact_check"
        ? collectFactCheckSubs(events)
        : [];

  if (!subs.length) {
    const withSelection = baseNodes.map((n) => ({ ...n, selected: selectedAgentId === n.id }));
    return { nodes: withSelection, edges: baseEdges };
  }

  const parentBase = baseNodes.find((n) => n.id === expandedParentId);
  if (!parentBase) {
    const withSelection = baseNodes.map((n) => ({ ...n, selected: selectedAgentId === n.id }));
    return { nodes: withSelection, edges: baseEdges };
  }

  const pst = statusByAgent[expandedParentId] ?? "idle";

  const childTargetPos = expandedParentId === "parallel_search" ? Position.Left : Position.Top;

  const childNodes: Node[] = subs.map((s) => ({
    id: s.nodeId,
    type: "subAgent",
    position: { x: 0, y: 0 },
    data: {
      title: s.title,
      detail: s.detail,
      status: pst,
      targetHandleSide: expandedParentId === "parallel_search" ? "left" : "top",
    },
    selected: selectedAgentId === s.nodeId,
    targetPosition: childTargetPos,
  }));

  const { x: px, y: py } = parentBase.position;
  if (expandedParentId === "parallel_search") {
    placeParallelSearchSubs(px, py, childNodes);
  } else {
    placeFactCheckSubs(px, py, childNodes);
  }

  const sourcePos =
    expandedParentId === "parallel_search" ? Position.Right : Position.Bottom;

  const patchedBase = baseNodes.map((n) => {
    const node: Node = { ...n, selected: selectedAgentId === n.id };
    if (n.id === expandedParentId) {
      node.sourcePosition = sourcePos;
    }
    return node;
  });

  const subEdges: Edge[] = childNodes.map((cn) => ({
    id: `${expandedParentId}->${cn.id}`,
    source: expandedParentId,
    target: cn.id,
    type: "default",
    animated: pst === "running",
    zIndex: 100,
    style: { stroke: "#cbd5e1", strokeWidth: 2.25 },
  }));

  return {
    nodes: [...patchedBase, ...childNodes],
    edges: [...baseEdges, ...subEdges],
  };
}

function SubAgentNode({ data, selected }: NodeProps) {
  const st = (data.status as "idle" | "running" | "done" | "failed") ?? "idle";
  const color = statusBorderColor(st);
  const thPos = data.targetHandleSide === "top" ? Position.Top : Position.Left;
  return (
    <>
      <Handle
        type="target"
        position={thPos}
        style={{
          opacity: 0.9,
          width: 10,
          height: 10,
          background: "#64748b",
          border: "2px solid #cbd5e1",
        }}
      />
      <div
        className={`rounded-xl px-2.5 py-2 bg-gradient-to-b from-slate-900 to-slate-950 text-left shadow-lg shadow-black/40 ${selected ? "ring-2 ring-sky-400 ring-offset-2 ring-offset-slate-950" : ""}`}
        style={{
          border: `2px solid ${color}`,
          width: SUB_CARD_W,
          boxShadow: `0 4px 14px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.04)`,
        }}
      >
        <div className="text-[11px] font-medium text-slate-100 leading-snug line-clamp-3">{String(data.title ?? "")}</div>
        <div className="text-[9px] text-slate-400 mt-1 leading-snug line-clamp-4">{String(data.detail ?? "")}</div>
      </div>
    </>
  );
}

const subAgentNodeTypes = { subAgent: SubAgentNode };

function RfPlusIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" aria-hidden>
      <path d="M32 18.133H18.133V32h-4.266V18.133H0v-4.266h13.867V0h4.266v13.867H32z" />
    </svg>
  );
}

function RfMinusIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 5" aria-hidden>
      <path d="M0 0h32v4.2H0z" />
    </svg>
  );
}

function RfFitViewIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 30" aria-hidden>
      <path d="M3.692 4.63c0-.53.4-.938.939-.938h5.215V0H4.708C2.13 0 0 2.054 0 4.63v5.216h3.692V4.631zM27.354 0h-5.2v3.692h5.17c.53 0 .984.4.984.939v5.215H32V4.631A4.624 4.624 0 0027.354 0zm.954 24.83c0 .532-.4.94-.939.94h-5.215v3.768h5.215c2.577 0 4.631-2.13 4.631-4.707v-5.139h-3.692v5.139zm-23.677.94c-.531 0-.939-.4-.939-.94v-5.138H0v5.139c0 2.577 2.13 4.707 4.708 4.707h5.138V25.77H4.631z" />
    </svg>
  );
}

function RfLockIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 25 32" aria-hidden>
      <path d="M21.333 10.667H19.81V7.619C19.81 3.429 16.38 0 12.19 0 8 0 4.571 3.429 4.571 7.619v3.048H3.048A3.056 3.056 0 000 13.714v15.238A3.056 3.056 0 003.048 32h18.285a3.056 3.056 0 003.048-3.048V13.714a3.056 3.056 0 00-3.048-3.047zM12.19 24.533a3.056 3.056 0 01-3.047-3.047 3.056 3.056 0 013.047-3.048 3.056 3.056 0 013.048 3.048 3.056 3.056 0 01-3.048 3.047zm4.724-13.866H7.467V7.619c0-2.59 2.133-4.724 4.723-4.724 2.591 0 4.724 2.133 4.724 4.724v3.048z" />
    </svg>
  );
}

function RfUnlockIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 25 32" aria-hidden>
      <path d="M21.333 10.667H19.81V7.619C19.81 3.429 16.38 0 12.19 0c-4.114 1.828-1.37 2.133.305 2.438 1.676.305 4.42 2.59 4.42 5.181v3.048H3.047A3.056 3.056 0 000 13.714v15.238A3.056 3.056 0 003.048 32h18.285a3.056 3.056 0 003.048-3.048V13.714a3.056 3.056 0 00-3.048-3.047zM12.19 24.533a3.056 3.056 0 01-3.047-3.047 3.056 3.056 0 013.047-3.048 3.056 3.056 0 013.048 3.048 3.056 3.056 0 01-3.048 3.047z" />
    </svg>
  );
}

function ViewportToolbar({
  viewportLocked,
  onToggleViewportLock,
}: {
  viewportLocked: boolean;
  onToggleViewportLock: () => void;
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const minZoomReached = useStore((s) => s.transform[2] <= s.minZoom);
  const maxZoomReached = useStore((s) => s.transform[2] >= s.maxZoom);

  const onZoomIn = () => {
    if (!viewportLocked) zoomIn();
  };
  const onZoomOut = () => {
    if (!viewportLocked) zoomOut();
  };
  const onFitView = () => {
    if (!viewportLocked) fitView();
  };

  return (
    <Controls
      showZoom={false}
      showFitView={false}
      showInteractive={false}
      className="!bg-slate-900 !border-slate-700"
    >
      <ControlButton
        type="button"
        className="react-flow__controls-zoomin"
        onClick={onZoomIn}
        disabled={viewportLocked || maxZoomReached}
        title="Zoom in"
        aria-label="Zoom in"
      >
        <RfPlusIcon />
      </ControlButton>
      <ControlButton
        type="button"
        className="react-flow__controls-zoomout"
        onClick={onZoomOut}
        disabled={viewportLocked || minZoomReached}
        title="Zoom out"
        aria-label="Zoom out"
      >
        <RfMinusIcon />
      </ControlButton>
      <ControlButton
        type="button"
        className="react-flow__controls-fitview"
        onClick={onFitView}
        disabled={viewportLocked}
        title="Fit view"
        aria-label="Fit view"
      >
        <RfFitViewIcon />
      </ControlButton>
      <ControlButton
        type="button"
        className="react-flow__controls-interactive"
        onClick={onToggleViewportLock}
        title={viewportLocked ? "Unlock viewport (pan & zoom)" : "Lock viewport (pan & zoom)"}
        aria-label={viewportLocked ? "Unlock viewport" : "Lock viewport"}
        aria-pressed={viewportLocked}
      >
        {viewportLocked ? <RfLockIcon /> : <RfUnlockIcon />}
      </ControlButton>
    </Controls>
  );
}

function AgentSwarmCanvas({
  nodes,
  edges,
  onNodeClick,
}: {
  nodes: Node[];
  edges: Edge[];
  onNodeClick: NodeMouseHandler;
}) {
  const [viewportLocked, setViewportLocked] = useState(false);

  const viewportLockProps = viewportLocked
    ? {
        panOnDrag: false,
        zoomOnScroll: false,
        zoomOnPinch: false,
        panOnScroll: false,
        zoomOnDoubleClick: false,
        nodesDraggable: false,
        nodesConnectable: false,
      }
    : {};

  return (
    <ReactFlowProvider>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodeTypes={subAgentNodeTypes}
        onNodeClick={onNodeClick}
        proOptions={{ hideAttribution: true }}
        {...viewportLockProps}
      >
        <Background gap={18} color="#1e293b" />
        <ViewportToolbar
          viewportLocked={viewportLocked}
          onToggleViewportLock={() => setViewportLocked((v) => !v)}
        />
      </ReactFlow>
    </ReactFlowProvider>
  );
}

function useSessionStream(sessionId: string | null, enabled: boolean) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [claimVerifiedEvents, setClaimVerifiedEvents] = useState<StreamEvent[]>([]);
  const [cost, setCost] = useState({ usd: 0, invocations: 0 });

  useEffect(() => {
    if (!sessionId) {
      setEvents([]);
      setClaimVerifiedEvents([]);
      setCost({ usd: 0, invocations: 0 });
    }
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId || !enabled) return;
    const es = openEventSource(sessionId, 0);
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as StreamEvent;
        setEvents((prev) => [...prev, data].slice(-400));
        if (data.event_type === "claim_verified") {
          setClaimVerifiedEvents((prev) => [...prev, data].slice(-200));
        }
        if (data.event_type === "cost_update") {
          const p = data.payload;
          setCost({
            usd: Number(p.total_usd ?? 0),
            invocations: Number(p.invocations ?? 0),
          });
        }
      } catch {
        /* ignore parse */
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [sessionId, enabled]);

  return { events, claimVerifiedEvents, cost };
}

function formatElapsed(ms: number): string {
  if (ms <= 0) return "0:00";
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function ClockGlyph({ className }: { className?: string }) {
  return (
    <svg className={className} xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 6v6l4 2" />
    </svg>
  );
}

function useRunElapsedMs(
  sessionId: string | null,
  createdAt: string | undefined,
  status: string | undefined,
  updatedAt: string | undefined,
) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!sessionId) return;
    if (status === "completed" || status === "failed") return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [sessionId, status]);

  return useMemo(() => {
    if (!sessionId || !createdAt) return 0;
    const start = new Date(createdAt).getTime();
    if (!Number.isFinite(start)) return 0;
    if (status === "completed" || status === "failed") {
      const endRaw = updatedAt ? new Date(updatedAt).getTime() : Date.now();
      const end = Number.isFinite(endRaw) ? endRaw : Date.now();
      return Math.max(0, end - start);
    }
    return Math.max(0, Date.now() - start);
  }, [sessionId, createdAt, status, updatedAt, tick]);
}

function deriveAgentStatuses(events: StreamEvent[]): Record<string, "idle" | "running" | "done" | "failed"> {
  const st: Record<string, "idle" | "running" | "done" | "failed"> = {};
  for (const e of events) {
    const p = e.payload as { agent_id?: string; ok?: boolean };
    if (e.event_type === "agent_started" && p.agent_id) {
      st[p.agent_id] = "running";
    }
    if (e.event_type === "agent_completed" && p.agent_id) {
      st[p.agent_id] = p.ok === false ? "failed" : "done";
    }
  }
  return st;
}

export default function App() {
  const [query, setQuery] = useState("What are the main trade-offs between async Postgres checkpointers and in-memory LangGraph state?");
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(SESSION_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof getResearch>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [expandedGraphParentId, setExpandedGraphParentId] = useState<string | null>(null);

  const streamEnabled = !!sessionId;
  const { events, claimVerifiedEvents, cost } = useSessionStream(sessionId, streamEnabled);

  useEffect(() => {
    try {
      if (sessionId) sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      else sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } catch {
      /* ignore quota / private mode */
    }
  }, [sessionId]);

  useEffect(() => {
    setExpandedGraphParentId(null);
  }, [sessionId]);

  const runElapsedMs = useRunElapsedMs(sessionId, detail?.created_at, detail?.status, detail?.updated_at);
  const canDownloadReport = Boolean(detail?.final_report?.trim());

  const graphClaimEvents = useMemo(
    () => streamEventsFromGraphVerifiedClaims(detail?.graph_state ?? null),
    [detail?.graph_state],
  );

  const claimStreamForMerge = graphClaimEvents.length > 0 ? graphClaimEvents : claimVerifiedEvents;

  const unifiedEvents = useMemo(
    () => mergeEventsKeepingClaimStream(events, claimStreamForMerge),
    [events, claimStreamForMerge],
  );

  const claimsForTrustPanel = useMemo(() => {
    if (graphClaimEvents.length > 0) return graphClaimEvents.slice(-50).reverse();
    return claimVerifiedEvents.slice(-50).reverse();
  }, [graphClaimEvents, claimVerifiedEvents]);

  const statuses = useMemo(() => deriveAgentStatuses(unifiedEvents), [unifiedEvents]);
  const { nodes, edges } = useMemo(
    () => buildAgentGraph(statuses, expandedGraphParentId, unifiedEvents, selectedAgent),
    [statuses, expandedGraphParentId, unifiedEvents, selectedAgent],
  );

  const onGraphNodeClick = useCallback<NodeMouseHandler>((_, n) => {
    const id = n.id;
    if (id.startsWith("sub:")) {
      setSelectedAgent(id);
      return;
    }
    if (isExpandableParent(id)) {
      setExpandedGraphParentId((prev) => (prev === id ? null : id));
    } else {
      setExpandedGraphParentId(null);
    }
    setSelectedAgent(id);
  }, []);

  const inspectorEvents = useMemo(() => {
    if (!selectedAgent) return unifiedEvents.slice(-80);

    const psMatch = /^sub:parallel_search:(.+)$/.exec(selectedAgent);
    if (psMatch) {
      const subId = psMatch[1];
      return unifiedEvents
        .filter((e) => {
          if (e.event_type !== "tool_call") return false;
          const p = e.payload as { parent_id?: string; agent_id?: string };
          return p.parent_id === "parallel_search" && String(p.agent_id) === subId;
        })
        .slice(-80);
    }

    const fcMatch = /^sub:fact_check:(.+)$/.exec(selectedAgent);
    if (fcMatch) {
      const cid = fcMatch[1];
      return unifiedEvents
        .filter((e) => {
          if (e.event_type !== "claim_verified") return false;
          const p = e.payload as { claim_id?: string };
          return String(p.claim_id) === cid;
        })
        .slice(-80);
    }

    if (selectedAgent === "parallel_search") {
      return unifiedEvents
        .filter((e) => {
          if (e.event_type === "tool_call") {
            const p = e.payload as { parent_id?: string };
            return p.parent_id === "parallel_search";
          }
          if (e.event_type === "agent_started" || e.event_type === "agent_completed") {
            const p = e.payload as { agent_id?: string };
            return p.agent_id === "parallel_search";
          }
          return false;
        })
        .slice(-80);
    }

    if (selectedAgent === "fact_check") {
      return unifiedEvents
        .filter((e) => {
          if (e.event_type === "claim_verified") return true;
          if (e.event_type === "agent_started" || e.event_type === "agent_completed") {
            const p = e.payload as { agent_id?: string };
            return p.agent_id === "fact_check";
          }
          return false;
        })
        .slice(-80);
    }

    return unifiedEvents
      .filter((e) => {
        const aid = (e.payload as { agent_id?: string }).agent_id;
        return aid === selectedAgent;
      })
      .slice(-80);
  }, [unifiedEvents, selectedAgent]);

  const onStart = useCallback(async () => {
    setError(null);
    try {
      const row = await createResearch(query);
      setSessionId(row.id);
      setDetail(row);
    } catch (e) {
      if (isUnreachableBackendError(e)) {
        setError(SERVER_UNREACHABLE_MESSAGE);
      } else {
        setError(String(e));
      }
    }
  }, [query]);

  const fetchDetail = useCallback(async () => {
    if (!sessionId) return;
    try {
      const row = await getResearch(sessionId);
      setDetail(row);
      setError((prev) => (prev === SERVER_UNREACHABLE_MESSAGE ? null : prev));
    } catch (e) {
      if (isUnreachableBackendError(e)) {
        setError(SERVER_UNREACHABLE_MESSAGE);
        return;
      }
      setError(String(e));
    }
  }, [sessionId]);

  /** Reloads row + graph context when `sessionId` is restored or changes. */
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const row = await getResearch(sessionId);
        if (!cancelled) {
          setDetail(row);
          setError((prev) => (prev === SERVER_UNREACHABLE_MESSAGE ? null : prev));
        }
      } catch (e) {
        if (!cancelled && isUnreachableBackendError(e)) {
          setError(SERVER_UNREACHABLE_MESSAGE);
          return;
        }
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const t = setInterval(fetchDetail, 4000);
    return () => clearInterval(t);
  }, [sessionId, fetchDetail]);

  /** Clears stored session (per user: Refresh ends persistence). */
  const onClearSession = useCallback(() => {
    setSessionId(null);
    setDetail(null);
    setError(null);
    setSelectedAgent(null);
  }, []);

  return (
    <div className="min-h-screen flex flex-col font-sans">
      <header className="border-b border-slate-800 px-6 py-4 flex flex-wrap gap-4 items-center bg-slate-900/80 backdrop-blur">
        <h1 className="text-lg font-semibold tracking-tight text-white">Deep Research Swarm</h1>
        <div className="flex flex-1 gap-2 items-center min-w-[240px]">
          <input
            className="flex-1 rounded-lg bg-slate-900 border border-slate-700 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Research question…"
          />
          <button
            type="button"
            onClick={onStart}
            className="rounded-lg bg-emerald-600 hover:bg-emerald-500 px-4 py-2 text-sm font-medium text-white"
          >
            Run
          </button>
          <button
            type="button"
            onClick={onClearSession}
            disabled={!sessionId}
            title="Clear the saved session for this tab (the browser also clears it when you close the tab)"
            className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-200 disabled:opacity-40"
          >
            Refresh
          </button>
        </div>
        <div className="font-mono text-xs text-amber-200/90 bg-slate-800/80 px-3 py-2 rounded-lg border border-slate-700">
          ${cost.usd.toFixed(4)} · {cost.invocations} invocations
        </div>
      </header>

      {error && (
        <div className="mx-6 mt-3 rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      <main className="flex-1 grid grid-cols-1 lg:grid-cols-3 gap-0 min-h-[640px]">
        <section className="lg:col-span-2 border-b lg:border-b-0 lg:border-r border-slate-800 min-h-[420px]">
          <AgentSwarmCanvas nodes={nodes} edges={edges} onNodeClick={onGraphNodeClick} />
        </section>

        <aside className="flex flex-col border-slate-800 bg-slate-900/40 min-h-0 lg:min-h-[640px]">
          <div className="border-b border-slate-800 px-4 py-2 text-xs uppercase tracking-wider text-slate-500 shrink-0">
            Inspector {selectedAgent ? `· ${selectedAgent}` : "· all agents"}
          </div>
          <div className="h-[350px] shrink-0 overflow-y-auto font-mono text-[11px] px-3 py-2 space-y-1 border-b border-slate-800/60 bg-slate-950/20">
            {inspectorEvents.map((e, i) => (
              <pre key={i} className="whitespace-pre-wrap break-all text-slate-400 border-b border-slate-800/60 pb-1">
                {e.event_type} {JSON.stringify(e.payload)}
              </pre>
            ))}
          </div>

          <div className="flex flex-col border-t border-slate-800 divide-y divide-slate-800 shrink-0 min-h-[200px] flex-1">
            <div className="flex flex-col gap-3 p-4 bg-slate-950/60 justify-center shrink-0">
              <div className="text-xs uppercase tracking-wider text-slate-500">Execution time</div>
              <div className="flex items-center gap-2 text-slate-100">
                <ClockGlyph className="shrink-0 text-amber-200/90" />
                <span className="font-mono text-2xl tabular-nums tracking-tight">{formatElapsed(runElapsedMs)}</span>
              </div>
              <p className="text-[11px] text-slate-500 leading-snug">Wall time from session start until completion (or live while running).</p>
              <button
                type="button"
                disabled={!canDownloadReport || !detail}
                onClick={async () => {
                  if (!detail) return;
                  try {
                    await downloadResearchPdf(detail, events, formatElapsed(runElapsedMs), claimVerifiedEvents);
                  } catch (e) {
                    setError(String(e));
                  }
                }}
                className={`rounded-lg px-3 py-2 text-sm font-medium text-center transition-colors ${
                  canDownloadReport && detail
                    ? "bg-emerald-600 hover:bg-emerald-500 text-white cursor-pointer"
                    : "border border-slate-600 bg-slate-800/50 text-slate-500 cursor-not-allowed opacity-50"
                }`}
              >
                Download PDF report
              </button>
            </div>
            <div className="flex flex-col shrink-0">
              <div className="border-b border-slate-800 px-4 py-2 text-xs uppercase tracking-wider text-slate-500 shrink-0">
                Trust stream (claim_verified)
              </div>
              <div className="h-[220px] shrink-0 overflow-y-auto px-3 py-2 space-y-2 bg-slate-950/20">
                {claimsForTrustPanel.map((c, i) => {
                  const p = c.payload as {
                    trust_score?: number;
                    claim?: string;
                    sources?: { url?: string; title?: string }[];
                  };
                  const t = p.trust_score ?? 0;
                  const color =
                    t >= 81 ? "text-emerald-300 border-emerald-900/50" : t >= 51 ? "text-amber-200 border-amber-900/40" : "text-red-300 border-red-900/40";
                  return (
                    <div key={i} className={`rounded-md border px-2 py-1 text-xs ${color}`}>
                      <div className="font-semibold">{t}/100</div>
                      <div className="text-slate-300 line-clamp-3">{p.claim}</div>
                      {p.sources?.length ? (
                        <ul className="mt-1 text-[10px] text-slate-500 list-disc pl-4 space-y-0.5">
                          {p.sources.slice(0, 4).map((s, j) => (
                            <li key={j} className="truncate">
                              {s.url ? (
                                <a href={s.url} className="underline hover:text-slate-300" target="_blank" rel="noreferrer">
                                  {s.title || s.url}
                                </a>
                              ) : (
                                s.title
                              )}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  );
                })}
                {!claimsForTrustPanel.length && (
                  <p className="text-xs text-slate-500">
                    {detail?.status === "completed"
                      ? "No verified claims for this run (extract_claims may have returned none, or fact-check was skipped)."
                      : "Waiting for claims…"}
                  </p>
                )}
              </div>
            </div>
          </div>
        </aside>
      </main>

      <footer className="border-t border-slate-800 px-6 py-4 bg-slate-950">
        <div className="text-xs text-slate-500 mb-2 flex gap-4">
          <span title={sessionId ?? undefined}>
            Session: {shortSessionLabel(sessionId)}
          </span>
          <span>Status: {detail?.status ?? "—"}</span>
        </div>
        <div className="prose prose-invert prose-sm max-w-none max-h-[360px] overflow-y-auto">
          {detail?.final_report ? (
            <ReactMarkdown>{detail.final_report}</ReactMarkdown>
          ) : (
            <p className="text-slate-500 text-sm">Report will appear when the run completes.</p>
          )}
        </div>
      </footer>
    </div>
  );
}
