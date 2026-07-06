import {
  Assessment,
  Band,
  DocumentRead,
  PHASE_KEYS,
  PHASE_LABELS,
  PhaseInfo,
  PhaseKey,
  PhaseState,
  ScenarioRead,
  ScenarioScoreRead,
  WeaknessRead,
} from "./types";

export function bandRank(band: Band): number {
  switch (band) {
    case "VeryHigh": return 4;
    case "High": return 3;
    case "Moderate": return 2;
    case "Low": return 1;
  }
}

export function severityRank(severity: WeaknessRead["severity"]): number {
  switch (severity) {
    case "critical": return 4;
    case "high": return 3;
    case "medium": return 2;
    case "low": return 1;
  }
}

// The single severity palette for the whole app. Low is deliberately gray,
// not green — a low-severity weakness is still a defect, and green reads as
// "good" when scanning a findings list.
export const SEVERITY_STYLES: Record<
  WeaknessRead["severity"],
  { dot: string; text: string; badge: string }
> = {
  critical: { dot: "bg-risk-veryhigh", text: "text-risk-veryhigh", badge: "bg-risk-veryhigh text-white" },
  high:     { dot: "bg-risk-high",     text: "text-risk-high",     badge: "bg-risk-high text-white" },
  medium:   { dot: "bg-amber-500",     text: "text-amber-700",     badge: "bg-amber-100 text-amber-800" },
  low:      { dot: "bg-ink-400",       text: "text-ink-500",       badge: "bg-ink-100 text-ink-700" },
};

export const SEVERITY_ORDER: WeaknessRead["severity"][] = ["critical", "high", "medium", "low"];

// Highest residual risk first; tiebreak on residual impact × likelihood, then code.
export function compareScenariosByRisk(a: ScenarioRead, b: ScenarioRead): number {
  const db = bandRank(b.score_band) - bandRank(a.score_band);
  if (db !== 0) return db;
  const sb = b.residual_impact * b.residual_likelihood - a.residual_impact * a.residual_likelihood;
  if (sb !== 0) return sb;
  return a.code.localeCompare(b.code);
}

export function compareScenarioScoresByRisk(a: ScenarioScoreRead, b: ScenarioScoreRead): number {
  const db = bandRank(b.band) - bandRank(a.band);
  if (db !== 0) return db;
  const sb = b.residual_impact * b.residual_likelihood - a.residual_impact * a.residual_likelihood;
  if (sb !== 0) return sb;
  return a.code.localeCompare(b.code);
}

export function compareWeaknessesBySeverity(a: WeaknessRead, b: WeaknessRead): number {
  const ds = severityRank(b.severity) - severityRank(a.severity);
  if (ds !== 0) return ds;
  return a.id - b.id;
}

export function bandColor(band: Band): string {
  switch (band) {
    case "Low": return "bg-risk-low";
    case "Moderate": return "bg-risk-moderate";
    case "High": return "bg-risk-high";
    case "VeryHigh": return "bg-risk-veryhigh";
  }
}

export function bandTextColor(band: Band): string {
  switch (band) {
    case "Low": return "text-risk-low";
    case "Moderate": return "text-risk-moderate";
    case "High": return "text-risk-high";
    case "VeryHigh": return "text-risk-veryhigh";
  }
}

export function bandLabel(band: Band): string {
  return band === "VeryHigh" ? "Very High" : band;
}

export function formatPercent(x: number): string {
  return `${Math.round(x * 100)}%`;
}

export function matchesQuery(text: string | null | undefined, q: string): boolean {
  if (!q) return true;
  if (!text) return false;
  return text.toLowerCase().includes(q.toLowerCase());
}

export interface DocumentFindingsGroup {
  doc: DocumentRead;
  weaknesses: WeaknessRead[];
}

// Group weaknesses by source document. Documents with zero weaknesses are
// included so the Evidence page still shows every uploaded file. Sort docs by
// finding count desc, then filename asc; weaknesses inside each group by severity.
export function groupWeaknessesByDocument(
  weaknesses: WeaknessRead[],
  documents: DocumentRead[],
): DocumentFindingsGroup[] {
  const byDoc = new Map<number, WeaknessRead[]>();
  for (const w of weaknesses) {
    if (w.source_document_id == null) continue;
    const list = byDoc.get(w.source_document_id) ?? [];
    list.push(w);
    byDoc.set(w.source_document_id, list);
  }
  const groups: DocumentFindingsGroup[] = documents.map((doc) => ({
    doc,
    weaknesses: (byDoc.get(doc.id) ?? []).slice().sort(compareWeaknessesBySeverity),
  }));
  groups.sort((a, b) => {
    const dn = b.weaknesses.length - a.weaknesses.length;
    if (dn !== 0) return dn;
    return a.doc.filename.localeCompare(b.doc.filename);
  });
  return groups;
}

export function severityCounts(
  weaknesses: WeaknessRead[],
): Record<WeaknessRead["severity"], number> {
  const counts = { critical: 0, high: 0, medium: 0, low: 0 };
  for (const w of weaknesses) counts[w.severity] += 1;
  return counts;
}

// ---------- Phase status ----------

export function phaseDotColor(state: PhaseState): string {
  switch (state) {
    case "done": return "bg-emerald-500";
    case "running": return "bg-amber-400 animate-pulse";
    case "error": return "bg-risk-high";
    case "pending": return "bg-ink-200";
  }
}

function getPhase(a: Assessment, key: PhaseKey): PhaseInfo {
  return a.phases?.[key] ?? {
    state: "pending",
    started_at: null,
    completed_at: null,
    task_id: null,
    error: null,
    detail: null,
    progress: null,
  };
}

// One-line summary for the listing row. Picks the most informative thing to
// say: in progress > error > all done > last completed.
export function phaseSummary(a: Assessment): string {
  const phases = PHASE_KEYS.map((k) => [k, getPhase(a, k)] as const);
  const running = phases.find(([, p]) => p.state === "running");
  if (running) {
    const [k, p] = running;
    const pct = p.progress != null ? ` (${Math.round(p.progress * 100)}%)` : "";
    return `Running: ${PHASE_LABELS[k]}${pct}`;
  }
  const errored = phases.find(([, p]) => p.state === "error");
  if (errored) {
    const [k] = errored;
    return `Failed: ${PHASE_LABELS[k]}`;
  }
  const done = phases.filter(([, p]) => p.state === "done");
  if (done.length === phases.length) return "All phases complete";
  if (done.length === 0) return `Pending: ${PHASE_LABELS[phases[0][0]]}`;
  const lastDone = done[done.length - 1][0];
  const nextPending = phases.find(([, p]) => p.state === "pending");
  if (!nextPending) return `Last completed: ${PHASE_LABELS[lastDone]}`;
  return `Last completed: ${PHASE_LABELS[lastDone]} · Pending: ${PHASE_LABELS[nextPending[0]]}`;
}

export function phaseList(a: Assessment): { key: PhaseKey; info: PhaseInfo }[] {
  return PHASE_KEYS.map((k) => ({ key: k, info: getPhase(a, k) }));
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} hr ago`;
  return new Date(iso).toLocaleString();
}
