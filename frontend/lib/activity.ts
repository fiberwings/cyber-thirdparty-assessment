"use client";

import { useEffect, useRef, useState } from "react";

// Human labels for task kinds (mirrors backend app.workflow.KIND_LABEL) plus
// the client-only kind used for the synchronous attestation-profile call.
export const KIND_LABEL: Record<string, string> = {
  scoping_turn: "Scoping",
  scenarios_generation: "Scenario generation",
  document_extraction: "Document extraction",
  cross_correlation: "Cross-correlation",
  gap_analysis: "Gap analysis",
  gap_analysis_control: "Per-control gap analysis",
  narratives: "Narratives & summary",
  executive_summary: "Executive summary",
  attestation_profile: "Attestation profile",
};

// Plain-language reading of the router's call purpose — the one signal that
// says what the model is being asked right now, independent of stage.
export const PURPOSE_LABEL: Record<string, string> = {
  scoping_turn: "Drafting the next scoping question",
  scenario_skeletons: "Drafting the risk scenarios",
  scenario_controls: "Selecting expected controls for a scenario",
  document_weakness_extract: "Extracting findings from the document",
  document_weakness_extract_window: "Extracting findings from a section window",
  document_weakness_enumerate: "Enumerating candidate findings",
  document_weakness_detail: "Detailing a finding with its evidence",
  attestation_profile: "Reading the attestation profile",
  weakness_confirmation: "Confirming findings against the whole bundle",
  weakness_merge: "Merging duplicate findings",
  cross_correlation: "Mapping a cluster of findings to controls",
  gap_analysis_control: "Assessing a control against the evidence",
  gap_analysis_control_r2: "Re-checking a control verdict",
  narrative: "Writing a scenario narrative",
  executive_summary: "Writing the executive summary",
};

export function purposeLabel(purpose: string | undefined): string | null {
  if (!purpose) return null;
  return PURPOSE_LABEL[purpose] ?? purpose.replace(/_/g, " ");
}

export function formatDuration(s: number): string {
  const t = Math.max(0, Math.floor(s));
  if (t < 60) return `${t}s`;
  const m = Math.floor(t / 60);
  const sec = t % 60;
  if (m < 60) return `${m}m ${sec.toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, "0")}m`;
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

// Elapsed seconds, anchored on the server's `elapsed_s` at the moment it
// was received and ticked locally between polls — so it survives
// navigation and re-attach, and is immune to client/server clock skew.
// `startedAtMs` (client clock) is the fallback for client-only sources.
export function useElapsed(elapsedS: number | null | undefined, startedAtMs?: number): number {
  const anchor = useRef<{ base: number; at: number } | null>(null);
  const [, tick] = useState(0);

  if (elapsedS != null) {
    // Re-anchor whenever the server value moves (each poll).
    const prev = anchor.current;
    if (!prev || prev.base !== elapsedS) anchor.current = { base: elapsedS, at: Date.now() };
  } else if (startedAtMs != null) {
    if (!anchor.current) anchor.current = { base: 0, at: startedAtMs };
  }

  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const a = anchor.current;
  if (!a) return 0;
  return a.base + (Date.now() - a.at) / 1000;
}
