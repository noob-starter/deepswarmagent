/**
 * Client-side research session report as PDF (models, timings, claims, sources).
 */

import autoTable from "jspdf-autotable";
import { jsPDF } from "jspdf";

import { fetchResearchEvents, type ResearchSession, type StreamEvent } from "./api";

function parseIso(s: string | null | undefined): number {
  if (!s) return NaN;
  const t = new Date(s).getTime();
  return Number.isFinite(t) ? t : NaN;
}

export function sortEventsChronologically(events: StreamEvent[]): StreamEvent[] {
  return [...events]
    .map((e, i) => ({ e, i }))
    .sort((a, b) => {
      const aid = a.e.id ?? a.i;
      const bid = b.e.id ?? b.i;
      return aid - bid;
    })
    .map(({ e }) => e);
}

/**
 * The main event ring buffer drops `claim_verified` when it caps length; replay the
 * deduped claim stream alongside so fact-check events are not lost.
 */
export function mergeEventsKeepingClaimStream(
  ringBufferEvents: StreamEvent[],
  claimVerifiedOnly: StreamEvent[],
): StreamEvent[] {
  const base = ringBufferEvents.filter((e) => e.event_type !== "claim_verified");
  return sortEventsChronologically([...base, ...claimVerifiedOnly]);
}

/** One worker under “Search wave” (same grouping as expanded graph UI). */
export type SearchWaveSubtask = {
  agentId: string;
  /** Sub-question copy from SSE `tool_call.args_summary`. */
  question: string;
  /** Same as UI subtitle: rails · aggregate hits across tools for that worker. */
  railsAndHits: string;
};

export function searchWaveSubtasksFromEvents(events: StreamEvent[]): SearchWaveSubtask[] {
  const sorted = sortEventsChronologically(events);
  const map = new Map<string, { question: string; tools: Set<string>; hits: number }>();
  for (const e of sorted) {
    if (e.event_type !== "tool_call") continue;
    const p = e.payload as {
      parent_id?: string;
      agent_id?: string;
      args_summary?: string;
      tool?: string;
      hits?: number;
    };
    if (p.parent_id !== "parallel_search" || !p.agent_id) continue;
    const aid = String(p.agent_id);
    if (!map.has(aid)) {
      map.set(aid, {
        question: String(p.args_summary ?? "").trim() || aid,
        tools: new Set<string>(),
        hits: 0,
      });
    }
    const row = map.get(aid)!;
    if (p.tool) row.tools.add(String(p.tool));
    row.hits += Number(p.hits) || 0;
    const as = String(p.args_summary ?? "").trim();
    if (as.length > row.question.length) row.question = as;
  }
  return Array.from(map.entries()).map(([agentId, v]) => ({
    agentId,
    question: v.question,
    railsAndHits: v.tools.size ? `${[...v.tools].join(", ")} · ${v.hits} hits` : "search worker",
  }));
}

export type AgentStepRow = {
  agent: string;
  label: string;
  durationSec: number;
  outcome: string;
};

export function buildAgentStepRows(events: StreamEvent[]): AgentStepRow[] {
  const sorted = sortEventsChronologically(events);
  const starts = new Map<string, { t: number; label: string }>();
  const rows: AgentStepRow[] = [];

  for (const e of sorted) {
    const p = e.payload as Record<string, unknown>;
    const tEnd = parseIso(e.created_at);

    if (e.event_type === "agent_started" && p.agent_id) {
      const id = String(p.agent_id);
      starts.set(id, {
        t: tEnd,
        label: String(p.label ?? id),
      });
    }

    if (e.event_type === "agent_completed" && p.agent_id) {
      const id = String(p.agent_id);
      const st = starts.get(id);
      let durationSec = 0;
      if (st && Number.isFinite(tEnd) && Number.isFinite(st.t)) {
        durationSec = Math.max(0, (tEnd - st.t) / 1000);
      }
      const outcome = p.ok === false ? "failed" : "ok";
      rows.push({
        agent: id,
        label: st?.label ?? id,
        durationSec: Math.round(durationSec * 100) / 100,
        outcome,
      });
    }
  }

  return rows;
}

function extractRunConfig(events: StreamEvent[]): Record<string, unknown> {
  const ev = events.find((e) => e.event_type === "run_config");
  return ev?.payload ?? {};
}

export function trustBandLabel(trust: number): "HIGH" | "MODERATE" | "LOW" {
  if (trust >= 81) return "HIGH";
  if (trust >= 51) return "MODERATE";
  return "LOW";
}

export type ClaimRow = {
  trust: number;
  claim: string;
  sourceLines: string;
  breakdown: Record<string, number> | null;
};

function claimRowsFromEvents(events: StreamEvent[]): ClaimRow[] {
  const fromEv: ClaimRow[] = [];
  for (const e of sortEventsChronologically(events)) {
    if (e.event_type !== "claim_verified") continue;
    const p = e.payload as {
      trust_score?: number;
      trust_breakdown?: Record<string, number>;
      claim?: string;
      sources?: { source_id?: string; url?: string; title?: string }[];
    };
    const lines =
      p.sources
        ?.map((s) => {
          const u = (s.url || "").trim();
          const tit = (s.title || "").trim();
          if (u) return tit ? `${tit} — ${u}` : u;
          return s.source_id ? String(s.source_id) : "";
        })
        .filter(Boolean) ?? [];
    const bd = p.trust_breakdown;
    const breakdown =
      bd && typeof bd === "object"
        ? (Object.fromEntries(
            Object.entries(bd).filter(([, v]) => typeof v === "number"),
          ) as Record<string, number>)
        : null;
    fromEv.push({
      trust: Number(p.trust_score ?? 0),
      claim: (p.claim ?? "").slice(0, 500),
      sourceLines: lines.join("\n"),
      breakdown,
    });
  }
  return fromEv;
}

function parseBreakdown(raw: unknown): Record<string, number> | null {
  if (!raw || typeof raw !== "object") return null;
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).filter(([, v]) => typeof v === "number"),
  ) as Record<string, number>;
}

/** Completed runs persist `verified_claims` on `graph_state` (authoritative for PDF/UI). */
export function claimRowsFromGraphVerified(graph: Record<string, unknown> | null | undefined): ClaimRow[] {
  const raw = graph?.verified_claims;
  if (!Array.isArray(raw)) return [];
  const cat = catalogFromGraphState(graph);
  const byId = new Map(cat.map((c) => [c.id, c]));
  const rows: ClaimRow[] = [];
  for (const c of raw) {
    if (!c || typeof c !== "object") continue;
    const o = c as Record<string, unknown>;
    const text = String(o.claim ?? "").trim();
    if (!text) continue;
    const ids = Array.isArray(o.source_ids) ? o.source_ids.map((x) => String(x)) : [];
    const lines: string[] = [];
    for (const sid of ids) {
      const src = byId.get(sid);
      if (!src) continue;
      const u = (src.url || "").trim();
      const tit = (src.title || "").trim();
      if (u) lines.push(tit ? `${tit} — ${u}` : u);
      else if (sid) lines.push(sid);
    }
    rows.push({
      trust: Number(o.trust_score ?? 0),
      claim: text.slice(0, 500),
      sourceLines: lines.join("\n"),
      breakdown: parseBreakdown(o.trust_breakdown),
    });
  }
  return rows;
}

function resolveClaimRowsForPdf(
  detail: ResearchSession,
  ringEvents: StreamEvent[],
  claimVerifiedRing: StreamEvent[],
): ClaimRow[] {
  const fromGraph = claimRowsFromGraphVerified(detail.graph_state ?? undefined);
  if (fromGraph.length > 0) return fromGraph;
  const merged = mergeEventsKeepingClaimStream(ringEvents, claimVerifiedRing);
  return claimRowsFromEvents(merged);
}

/** Synthetic SSE-shaped events for Trust UI when loading a completed session from REST. */
export function streamEventsFromGraphVerifiedClaims(
  graph: Record<string, unknown> | null | undefined,
): StreamEvent[] {
  const raw = graph?.verified_claims;
  if (!Array.isArray(raw) || !raw.length) return [];
  const cat = catalogFromGraphState(graph);
  const byId = new Map(cat.map((c) => [c.id, c]));
  const out: StreamEvent[] = [];
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    if (!c || typeof c !== "object") continue;
    const o = c as Record<string, unknown>;
    const cid = String(o.id ?? "");
    const ids = Array.isArray(o.source_ids) ? o.source_ids.map((x) => String(x)) : [];
    const sources = ids.map((sid) => {
      const src = byId.get(sid);
      return {
        source_id: sid,
        url: src?.url || "",
        title: src?.title || "",
      };
    });
    out.push({
      id: 1_000_000 + i,
      event_type: "claim_verified",
      payload: {
        claim_id: cid,
        trust_score: Number(o.trust_score ?? 0),
        trust_breakdown: o.trust_breakdown && typeof o.trust_breakdown === "object" ? o.trust_breakdown : {},
        claim: String(o.claim ?? "").slice(0, 400),
        source_ids: ids,
        sources,
      },
    });
  }
  return out;
}

function catalogFromGraphState(graph: Record<string, unknown> | null | undefined): { id: string; title: string; url: string }[] {
  if (!graph) return [];
  const raw = graph.all_sources;
  if (!Array.isArray(raw)) return [];
  const out: { id: string; title: string; url: string }[] = [];
  for (const s of raw) {
    if (!s || typeof s !== "object") continue;
    const o = s as Record<string, unknown>;
    const id = String(o.source_id ?? "");
    if (!id) continue;
    out.push({
      id,
      title: String(o.title ?? ""),
      url: String(o.url ?? ""),
    });
  }
  return out;
}

function stripMarkdownLight(md: string): string {
  return md
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[(.*?)]\((.*?)\)/g, "$1 ($2)");
}

/** Extract body of first `## Title` section (through next `##` or EOF). */
function extractMarkdownSection(md: string, sectionTitle: string): string | null {
  const esc = sectionTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^##\\s+${esc}\\s*$`, "im");
  const m = md.match(re);
  if (!m || m.index === undefined) return null;
  const tail = md.slice(m.index + m[0].length);
  const next = tail.search(/^##\s+/m);
  const section = (next === -1 ? tail : tail.slice(0, next)).trim();
  return section.length ? section : null;
}

/** Remove a `## Title` section from markdown (heading + body until next `##`). */
function stripMarkdownSection(md: string, sectionTitle: string): string {
  const esc = sectionTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^##\\s+${esc}\\s*$`, "im");
  const found = md.match(re);
  if (!found || found.index === undefined) return md;
  const start = found.index;
  const tail = md.slice(start + found[0].length);
  const next = tail.search(/^##\s+/m);
  const after = next === -1 ? "" : tail.slice(next);
  const before = md.slice(0, start).trimEnd();
  return (before + (after ? `\n\n${after}` : "")).trim();
}

/** Cap word count for the PDF executive summary (long sections are cut at maxWords). */
function clampWordCount(text: string, maxWords: number): { text: string; words: number } {
  const w = text.trim().split(/\s+/).filter(Boolean);
  if (w.length <= maxWords) {
    return { text: w.join(" "), words: w.length };
  }
  const take = maxWords;
  return { text: `${w.slice(0, take).join(" ")}…`, words: take };
}

/**
 * Align with backend `Settings.max_sub_questions_per_wave` default
 * (`backend/app/config.py`): first planner wave consumes at most this many sub-questions.
 */
const MAX_SUB_QUESTIONS_PER_WAVE = 8;

/** Planner JSON `sub_questions[].text`, same order as the first search wave when lengths match. */
function subQuestionTextsFromPlan(plan: unknown): string[] {
  if (!plan || typeof plan !== "object") return [];
  const raw = (plan as Record<string, unknown>).sub_questions;
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const text = String((item as Record<string, unknown>).text ?? "").trim();
    if (text) out.push(text);
  }
  return out;
}

/** One summarized finding per parallel search wave; pairs with planner sub-questions when available. */
export type IntermediateBreakdownRow = {
  question: string;
  summaryShort: string;
};

export function intermediateBreakdownRowsFromGraph(
  graph: Record<string, unknown> | null | undefined,
  summaryMaxWords = 120,
): IntermediateBreakdownRow[] {
  if (!graph) return [];
  const raw = graph.findings_summaries;
  if (!Array.isArray(raw)) return [];
  const summaries = raw.filter((x): x is string => typeof x === "string" && String(x).trim().length > 0);
  if (!summaries.length) return [];

  const planTexts = subQuestionTextsFromPlan(graph.plan);
  const wave1Count = Math.min(planTexts.length, MAX_SUB_QUESTIONS_PER_WAVE);
  const wave1Qs = planTexts.slice(0, wave1Count);

  return summaries.map((summary, i) => {
    let question: string;
    if (i < wave1Qs.length) {
      question = wave1Qs[i]!;
    } else if (!wave1Qs.length) {
      question = `Research sub-task ${i + 1}`;
    } else {
      question = `Follow-up sub-question #${i - wave1Qs.length + 1}`;
    }
    const plain = stripMarkdownLight(summary.trim());
    const { text: summaryShort } = clampWordCount(plain, summaryMaxWords);
    return { question, summaryShort };
  });
}

function appendSearchWaveQuestionsSection(
  doc: jsPDF,
  events: StreamEvent[],
  margin: number,
  contentW: number,
  y: number,
): number {
  const subs = searchWaveSubtasksFromEvents(events);
  if (!subs.length) return y;

  if (y > 215) {
    doc.addPage();
    y = 16;
  }
  doc.setFontSize(11);
  doc.text("Search wave questions", margin, y);
  y += 3;
  doc.setFontSize(7.5);
  doc.setTextColor(75);
  const noteLines = doc.splitTextToSize(
    "Same wording as expanding “Search wave” on the graph (sub-questions from SSE tool_call events; export loads full history from the API).",
    contentW,
  );
  doc.text(noteLines, margin, y);
  doc.setTextColor(0);
  y += Math.max(noteLines.length, 1) * 3 + 4;

  autoTable(doc, {
    startY: y,
    head: [["Question", "Search rails / hits"]],
    body: subs.map((s) => [s.question, s.railsAndHits]),
    styles: { fontSize: 7.5, cellPadding: 1.5, valign: "top" },
    columnStyles: {
      0: { cellWidth: Math.min(104, Math.floor(contentW * 0.64)) },
      1: { cellWidth: Math.max(44, Math.floor(contentW * 0.34)) },
    },
    margin: { left: margin, right: margin },
  });
  return (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
}

function appendIntermediateStatementBreakdowns(
  doc: jsPDF,
  graph: Record<string, unknown> | null | undefined,
  margin: number,
  contentW: number,
  y: number,
): number {
  const rows = intermediateBreakdownRowsFromGraph(graph);
  if (!rows.length) return y;

  if (y > 218) {
    doc.addPage();
    y = 16;
  }
  doc.setFontSize(11);
  doc.text("Intermediate statement breakdowns", margin, y);
  y += 3;
  doc.setFontSize(7.5);
  doc.setTextColor(75);
  const noteLines = doc.splitTextToSize(
    "Planner sub-questions matched to search-wave summaries when available; numbered rows are critic follow-ups.",
    contentW,
  );
  doc.text(noteLines, margin, y);
  doc.setTextColor(0);
  y += Math.max(noteLines.length, 1) * 3 + 4;

  autoTable(doc, {
    startY: y,
    head: [["Question", "Summarized response"]],
    body: rows.map((r) => [r.question, r.summaryShort]),
    styles: { fontSize: 7.5, cellPadding: 1.5, valign: "top" },
    columnStyles: { 0: { cellWidth: Math.min(72, contentW * 0.38) }, 1: { cellWidth: Math.max(40, contentW * 0.58) } },
    margin: { left: margin, right: margin },
  });
  return (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
}

function pickExecutiveSummarySource(md: string): { source: string; label: string } | null {
  const summary = extractMarkdownSection(md, "Summary");
  if (summary) return { source: summary, label: "Summary" };
  const answer = extractMarkdownSection(md, "Answer to your question");
  if (answer) return { source: answer, label: "Answer to your question" };
  return null;
}

/** Append wrapped text; returns new y position. */
function textBlock(doc: jsPDF, text: string, x: number, y: number, maxWidth: number, lineHeight: number): number {
  const lines = doc.splitTextToSize(text, maxWidth);
  doc.text(lines, x, y);
  return y + lines.length * lineHeight + 2;
}

const EXEC_SUMMARY_MIN = 200;
const EXEC_SUMMARY_MAX = 250;

/**
 * Short answer (~200–250 words) above the full report; full report omits the section
 * used for the summary so it is not printed twice.
 */
function appendFinalReportSection(
  doc: jsPDF,
  finalReport: string,
  margin: number,
  contentW: number,
  y: number,
): number {
  const picked = pickExecutiveSummarySource(finalReport);
  if (picked) {
    const plainSrc = stripMarkdownLight(picked.source);
    const { text: execPlain, words: wc } = clampWordCount(plainSrc, EXEC_SUMMARY_MAX);
    if (y > 232) {
      doc.addPage();
      y = 16;
    }
    doc.setFontSize(11);
    doc.text("Executive summary", margin, y);
    y += 5;
    doc.setFontSize(7.5);
    doc.setTextColor(75);
    doc.text(`(${wc} words, condensed from “${picked.label}”; target length ${EXEC_SUMMARY_MIN}–${EXEC_SUMMARY_MAX} words)`, margin, y);
    doc.setTextColor(0);
    y += 5;
    doc.setFontSize(8.5);
    const execChunks = execPlain.match(/[\s\S]{1,3500}/g) ?? [execPlain];
    for (const chunk of execChunks) {
      y = textBlock(doc, chunk, margin, y, contentW, 3.3);
      if (y > 278) {
        doc.addPage();
        y = 16;
      }
    }
    y += 6;
    if (picked.label === "Summary") {
      finalReport = stripMarkdownSection(finalReport, "Summary");
    } else {
      finalReport = stripMarkdownSection(finalReport, "Answer to your question");
    }
  }

  if (y > 240) {
    doc.addPage();
    y = 16;
  }
  doc.setFontSize(11);
  doc.text("Research answer & report", margin, y);
  y += 6;
  doc.setFontSize(8);
  const plain = stripMarkdownLight(finalReport);
  const chunks = plain.match(/[\s\S]{1,3500}/g) ?? [plain];
  for (const chunk of chunks) {
    y = textBlock(doc, chunk, margin, y, contentW, 3.2);
    if (y > 280) {
      doc.addPage();
      y = 16;
    }
  }
  return y + 4;
}

const TRUST_SCORE_EXPLAIN = [
  "Each verified claim receives a headline trust score from 0 (low) to 100 (high). In the agent pipeline, evidence for synthesis is labeled HIGH, MODERATE, or LOW from that score: HIGH when score ≥ 81, MODERATE when 51–80, LOW when 0–50.",
  "The headline score is a weighted blend of five dimensions (see per-claim table below when available): source_count (weight 15%) — more cited sources increase the score up to a cap; source_authority (25%) — higher for strong domains (.gov, .mil, .edu, Wikipedia, academic and major hubs); source_agreement (15%) — neutral placeholder until an entailment model is added; recency (15%) — newer publication dates score higher; fact_checker (30%) — score from the verification pass on claim–source alignment. If a claim has no sources, the module uses a reduced, fact-checker-heavy formula with a low cap.",
];

function appendTrustScoreAppendix(doc: jsPDF, claims: ClaimRow[], margin: number, contentW: number, y: number): number {
  if (y > 230) {
    doc.addPage();
    y = 16;
  }
  doc.setFontSize(11);
  doc.text("Trust score — methodology & session summary", margin, y);
  y += 6;
  doc.setFontSize(8);
  doc.setTextColor(40);
  for (const para of TRUST_SCORE_EXPLAIN) {
    y = textBlock(doc, para, margin, y, contentW, 3.4);
    y += 2;
    if (y > 275) {
      doc.addPage();
      y = 16;
    }
  }
  doc.setTextColor(0);
  y += 2;

  if (!claims.length) {
    y = textBlock(
      doc,
      "No verified claims were recorded for this session, so there are no per-claim trust scores or breakdowns in the export.",
      margin,
      y,
      contentW,
      3.4,
    );
    return y + 4;
  }

  const high = claims.filter((c) => trustBandLabel(c.trust) === "HIGH").length;
  const mod = claims.filter((c) => trustBandLabel(c.trust) === "MODERATE").length;
  const low = claims.filter((c) => trustBandLabel(c.trust) === "LOW").length;
  const avg = claims.reduce((s, c) => s + c.trust, 0) / claims.length;
  const summary = `This session: ${claims.length} verified claim(s). Mean headline score: ${avg.toFixed(1)}/100. Bands: HIGH ${high}, MODERATE ${mod}, LOW ${low}.`;
  y = textBlock(doc, summary, margin, y, contentW, 3.4);
  y += 4;
  if (y > 240) {
    doc.addPage();
    y = 16;
  }

  doc.setFontSize(9);
  doc.text("Per-claim score components (0–100 each, before weighting)", margin, y);
  y += 4;
  doc.setFontSize(7);

  const dim = (b: Record<string, number> | null, k: string) => {
    if (!b || typeof b[k] !== "number") return "—";
    return String(Math.round(b[k] * 10) / 10);
  };

  const wFixed = 16 + 14 + 14 + 16 + 14 + 14 + 14;

  autoTable(doc, {
    startY: y,
    head: [["Band", "Headline", "src_cnt", "authority", "agree", "recency", "fact_chk", "Claim (excerpt)"]],
    body: claims.map((c) => [
      trustBandLabel(c.trust),
      String(c.trust),
      dim(c.breakdown, "source_count"),
      dim(c.breakdown, "source_authority"),
      dim(c.breakdown, "source_agreement"),
      dim(c.breakdown, "recency"),
      dim(c.breakdown, "fact_checker"),
      c.claim.slice(0, 220),
    ]),
    styles: { fontSize: 6.5, cellPadding: 1.2, valign: "top" },
    columnStyles: {
      0: { cellWidth: 16 },
      1: { cellWidth: 14 },
      2: { cellWidth: 14 },
      3: { cellWidth: 16 },
      4: { cellWidth: 14 },
      5: { cellWidth: 14 },
      6: { cellWidth: 14 },
      7: { cellWidth: Math.max(36, contentW - wFixed) },
    },
    margin: { left: margin, right: margin },
  });
  return (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
}

export function buildResearchPdfBlob(
  detail: ResearchSession,
  events: StreamEvent[],
  elapsedLabel: string,
  claimVerifiedEvents: StreamEvent[] = [],
): Blob {
  const doc = new jsPDF({ unit: "mm", format: "a4" });
  const pageW = doc.internal.pageSize.getWidth();
  const margin = 14;
  const contentW = pageW - margin * 2;
  let y = 16;

  doc.setFontSize(16);
  doc.text("Deep Research Swarm — session report", margin, y);
  y += 8;
  doc.setFontSize(9);
  doc.setTextColor(80);
  doc.text(`Generated: ${new Date().toISOString().slice(0, 19)}Z`, margin, y);
  doc.setTextColor(0);
  y += 6;

  doc.setFontSize(11);
  doc.text("Session", margin, y);
  y += 5;
  doc.setFontSize(9);
  const metaLines = [
    `Session ID: ${detail.id}`,
    `Status: ${detail.status}`,
    `Created: ${detail.created_at}`,
    `Wall-clock duration: ${elapsedLabel}`,
    `Total cost (USD): ${detail.total_cost_usd}`,
    `Agent invocations (counter): ${detail.agent_invocation_count}`,
  ];
  for (const line of metaLines) {
    doc.text(line, margin, y);
    y += 4;
  }
  y += 4;

  doc.setFontSize(11);
  doc.text("Research question", margin, y);
  y += 5;
  doc.setFontSize(9);
  y = textBlock(doc, detail.query, margin, y, contentW, 4);
  y += 4;

  if (detail.final_report) {
    y = appendFinalReportSection(doc, detail.final_report, margin, contentW, y);
  }

  y = appendSearchWaveQuestionsSection(doc, events, margin, contentW, y);
  y = appendIntermediateStatementBreakdowns(doc, detail.graph_state ?? undefined, margin, contentW, y);

  const cfg = extractRunConfig(events);
  if (Object.keys(cfg).length) {
    doc.setFontSize(11);
    doc.text("Models & run parameters", margin, y);
    y += 2;
    autoTable(doc, {
      startY: y,
      head: [["Parameter", "Value"]],
      body: Object.entries(cfg).map(([k, v]) => [k, String(v)]),
      styles: { fontSize: 8, cellPadding: 1.5 },
      margin: { left: margin, right: margin },
    });
    y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
  }

  const steps = buildAgentStepRows(events);
  if (steps.length) {
    if (y > 250) {
      doc.addPage();
      y = 16;
    }
    doc.setFontSize(11);
    doc.text("Agent steps (wall time per node)", margin, y);
    y += 2;
    autoTable(doc, {
      startY: y,
      head: [["Agent id", "Label", "Duration (s)", "Outcome"]],
      body: steps.map((r) => [r.agent, r.label, String(r.durationSec), r.outcome]),
      styles: { fontSize: 8, cellPadding: 1.5 },
      columnStyles: { 0: { cellWidth: 38 }, 2: { cellWidth: 22 }, 3: { cellWidth: 18 } },
      margin: { left: margin, right: margin },
    });
    y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
  }

  const claims = resolveClaimRowsForPdf(detail, events, claimVerifiedEvents);
  if (claims.length) {
    if (y > 200) {
      doc.addPage();
      y = 16;
    }
    doc.setFontSize(11);
    doc.text("Verified claims & trust", margin, y);
    y += 2;
    autoTable(doc, {
      startY: y,
      head: [["Trust (band / score)", "Claim (excerpt)", "Cited / verified sources"]],
      body: claims.map((c) => [`${trustBandLabel(c.trust)} (${c.trust})`, c.claim, c.sourceLines || "—"]),
      styles: { fontSize: 7, cellPadding: 1.5, valign: "top" },
      columnStyles: { 0: { cellWidth: 28 }, 1: { cellWidth: 68 }, 2: { cellWidth: 78 } },
      margin: { left: margin, right: margin },
    });
    y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
  }

  const catalog = catalogFromGraphState(detail.graph_state ?? undefined);
  const uniqueByUrl = [...new Map(catalog.filter((c) => c.url).map((c) => [c.url, c])).values()];
  if (uniqueByUrl.length) {
    if (y > 220) {
      doc.addPage();
      y = 16;
    }
    doc.setFontSize(11);
    doc.text("Source catalog (URLs from graph state)", margin, y);
    y += 2;
    autoTable(doc, {
      startY: y,
      head: [["Source id", "Title", "URL"]],
      body: uniqueByUrl.map((c) => [c.id, c.title.slice(0, 120), c.url]),
      styles: { fontSize: 7, cellPadding: 1.5 },
      columnStyles: { 0: { cellWidth: 22 }, 1: { cellWidth: 65 }, 2: { cellWidth: 85 } },
      margin: { left: margin, right: margin },
    });
    y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 8;
  }

  y = appendTrustScoreAppendix(doc, claims, margin, contentW, y);

  return doc.output("blob");
}

export async function downloadResearchPdf(
  detail: ResearchSession,
  events: StreamEvent[],
  elapsedLabel: string,
  claimVerifiedEvents: StreamEvent[] = [],
): Promise<void> {
  let merged = mergeEventsKeepingClaimStream(sortEventsChronologically(events), claimVerifiedEvents);
  try {
    const persisted = sortEventsChronologically(await fetchResearchEvents(detail.id));
    if (persisted.length > 0) {
      merged = mergeEventsKeepingClaimStream(persisted, claimVerifiedEvents);
    }
  } catch {
    /* older backend or offline — use SSE buffer only */
  }
  const blob = buildResearchPdfBlob(detail, merged, elapsedLabel, claimVerifiedEvents);
  const safeId = String(detail.id).slice(0, 8);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `research-session-${safeId}.pdf`;
  a.click();
  URL.revokeObjectURL(a.href);
}
