"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Assessment, BlockReason, PhaseInfo, StaleInfo, WorkflowKey } from "./types";
import { WORKFLOW_KEYS } from "./types";

export interface StepView {
  info: PhaseInfo | undefined;
  isRunning: boolean;
  // Whether the step's action button should be enabled. Pure read-through of
  // the backend's `ready` (the rules live in backend app.workflow only).
  canRun: boolean;
  // First blocker's message when not ready, else null.
  blockedReason: string | null;
  blockedBy: BlockReason[];
  stale: StaleInfo | null;
}

// Reads the assessment's phases (same query key as AssessmentShell, so the
// cache entry is shared) and exposes per-step gating derived from the
// backend's `ready` / `blocked_by` / `stale` fields. Polls while any step is
// running so gates open as soon as a job lands.
export function useWorkflow(aid: number) {
  const { data: assessment } = useQuery({
    queryKey: ["assessment", aid],
    queryFn: () => api.getAssessment(aid),
    refetchInterval: (q) => {
      const a = q.state.data as Assessment | undefined;
      return anyRunning(a) ? 1500 : false;
    },
  });

  const step = (key: WorkflowKey): StepView => {
    const info = assessment?.phases?.[key];
    const isRunning = info?.state === "running";
    const blockedBy = info?.blocked_by ?? [];
    // `ready` is undefined until the assessment loads: keep buttons off.
    const ready = info?.ready === true;
    return {
      info,
      isRunning,
      canRun: ready && !isRunning,
      blockedReason: !ready ? blockedBy[0]?.message ?? (assessment ? "Not available yet." : null) : null,
      blockedBy,
      stale: info?.stale ?? null,
    };
  };

  return { assessment, step, anyRunning: anyRunning(assessment) };
}

export function anyRunning(a: Assessment | undefined): boolean {
  return WORKFLOW_KEYS.some((k) => a?.phases?.[k]?.state === "running");
}

// One line for a stale stamp, e.g. "Stale — Document uploaded: soc2.pdf".
export function staleLine(stale: StaleInfo | null | undefined): string | null {
  if (!stale) return null;
  return `Stale — ${stale.reasons.join("; ") || "inputs changed"}`;
}
