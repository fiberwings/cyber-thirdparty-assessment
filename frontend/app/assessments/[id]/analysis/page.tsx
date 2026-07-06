"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, pollTask } from "@/lib/api";
import { use, useEffect, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
import { compareScenariosByRisk, relativeTime } from "@/lib/utils";
import type { Assessment, PhaseInfo, PhaseKey } from "@/lib/types";
import { useRouter } from "next/navigation";

export default function AnalysisPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const qc = useQueryClient();
  const router = useRouter();

  const { data: scenarios } = useQuery({
    queryKey: ["scenarios", aid],
    queryFn: () => api.listScenarios(aid),
  });

  // Read live phase status from the same assessment query AssessmentShell uses.
  // Refetch every 1.5s while any of the three phases is running so the page
  // stays live even if the user reloaded mid-run.
  const { data: assessment } = useQuery({
    queryKey: ["assessment", aid],
    queryFn: () => api.getAssessment(aid),
    refetchInterval: (q) => {
      const a = q.state.data as Assessment | undefined;
      const running = (["correlation", "analysis", "score"] as (PhaseKey | "correlation")[]).some(
        (k) => a?.phases?.[k]?.state === "running",
      );
      return running ? 1500 : false;
    },
  });

  const [selectedId, setSelectedId] = useState<number | null>(null);

  // After a mutation completes, refresh the assessment so the green checkmark
  // and timestamp render. If the page is loaded while a job is already in
  // flight, attach to the existing task_id so we still drive a refetch on done.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["assessment", aid] });
    qc.invalidateQueries({ queryKey: ["scenarios", aid] });
    qc.invalidateQueries({ queryKey: ["weaknesses", aid] });
    qc.invalidateQueries({ queryKey: ["report", aid] });
  };

  const runGap = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.runGapAnalysis(aid);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await pollTask(task_id, undefined, 800);
      await api.recalculate(aid);
    },
    onSettled: invalidate,
  });

  const synthesize = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.synthesizeWeaknesses(aid);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await pollTask(task_id, undefined, 800);
    },
    onSettled: invalidate,
  });

  const writeNarratives = useMutation({
    mutationFn: async () => {
      await api.recalculate(aid);
      const { task_id } = await api.runNarratives(aid);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await pollTask(task_id, undefined, 800);
    },
    onSuccess: () => {
      router.push(`/assessments/${aid}/score`);
    },
    onSettled: invalidate,
  });

  // Auto-attach to running tasks discovered via phase_state on first render —
  // means a refresh mid-run still refetches once the task lands.
  useEffect(() => {
    const tasksToWatch = (["correlation", "analysis", "score"] as (PhaseKey | "correlation")[])
      .map((k) => assessment?.phases?.[k])
      .filter((p): p is PhaseInfo => !!p && p.state === "running" && !!p.task_id)
      .map((p) => p.task_id as string);
    if (tasksToWatch.length === 0) return;
    let cancelled = false;
    Promise.all(
      tasksToWatch.map((tid) => pollTask(tid, undefined, 1200).catch(() => undefined)),
    ).then(() => {
      if (!cancelled) invalidate();
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assessment?.phases?.analysis?.task_id, assessment?.phases?.score?.task_id, assessment?.phases?.correlation?.task_id]);

  const sortedScenarios = scenarios ? [...scenarios].sort(compareScenariosByRisk) : undefined;
  const selected = scenarios?.find((s) => s.id === selectedId) || null;

  const gapInfo = assessment?.phases?.analysis;
  const synthInfo = assessment?.phases?.correlation;
  const narrInfo = assessment?.phases?.score;

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Gap analysis</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        For each expected control, the reasoner retrieves candidate evidence and decides coverage and effectiveness.
        Every claim must be cited.
      </p>

      <div className="rounded-lg border border-ink-200 bg-white p-5 max-w-3xl space-y-4">
        <Step
          title="1. Run gap analysis"
          desc="Reasoner walks every scenario × expected control and pulls evidence from your uploaded documents."
          info={gapInfo}
          submitting={runGap.isPending}
          onRun={() => runGap.mutate()}
        />
        <div className="border-t border-ink-100" />
        <Step
          title="2. Cross-correlate weaknesses"
          desc="Maps extracted findings onto scenario controls and spawns emergent scenarios for risks the original scoping missed."
          info={synthInfo}
          submitting={synthesize.isPending}
          onRun={() => synthesize.mutate()}
        />
        <div className="border-t border-ink-100" />
        <Step
          title="3. Generate narratives & summary, then continue"
          desc="Recalculates residual risk, writes the score-explanation prose per scenario and the executive summary."
          info={narrInfo}
          submitting={writeNarratives.isPending}
          onRun={() => writeNarratives.mutate()}
        />
      </div>

      <div className="mt-8">
        <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Per-scenario results</div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {sortedScenarios?.map((s) => (
            <ScenarioCard key={s.id} scenario={s} onOpen={() => setSelectedId(s.id)} />
          ))}
        </div>
      </div>

      {selected && (
        <ScenarioDrawer
          scenario={selected}
          assessmentId={aid}
          onClose={() => setSelectedId(null)}
        />
      )}
    </AssessmentShell>
  );
}

function StatusIcon({ state }: { state: PhaseInfo["state"] | undefined }) {
  switch (state) {
    case "done":
      return (
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500 text-white text-[11px] font-bold">
          ✓
        </span>
      );
    case "running":
      return (
        <span className="flex h-5 w-5 items-center justify-center rounded-full border-2 border-amber-400 border-t-transparent animate-spin" />
      );
    case "error":
      return (
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-risk-high text-white text-[11px] font-bold">
          !
        </span>
      );
    case "pending":
    default:
      return <span className="flex h-5 w-5 items-center justify-center rounded-full border-2 border-ink-300" />;
  }
}

function statusLine(info: PhaseInfo | undefined): { text: string; cls: string } {
  if (!info || info.state === "pending") return { text: "Not run yet", cls: "text-ink-500" };
  if (info.state === "running") {
    const pct = info.progress != null ? `${Math.round(info.progress * 100)}%` : "running";
    const det = info.detail ? ` · ${info.detail}` : "";
    const since = info.started_at ? ` · started ${relativeTime(info.started_at)}` : "";
    return { text: `${pct}${det}${since}`, cls: "text-amber-700" };
  }
  if (info.state === "error") {
    return { text: info.error || "Failed — try again", cls: "text-risk-high" };
  }
  // done
  return {
    text: info.completed_at ? `Completed ${new Date(info.completed_at).toLocaleString()}` : "Completed",
    cls: "text-ink-500",
  };
}

function buttonLabel(info: PhaseInfo | undefined, submitting: boolean): string {
  if (submitting) return "Running…";
  if (info?.state === "running") return "Running…";
  if (info?.state === "done") return "Re-run";
  if (info?.state === "error") return "Retry";
  return "Run";
}

function Step({
  title, desc, info, submitting, onRun,
}: {
  title: string;
  desc: string;
  info: PhaseInfo | undefined;
  submitting: boolean;
  onRun: () => void;
}) {
  const status = statusLine(info);
  const isRunning = submitting || info?.state === "running";
  return (
    <div className="flex items-start gap-3">
      <div className="pt-0.5">
        <StatusIcon state={info?.state} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-ink-900">{title}</div>
        <div className="text-xs text-ink-600 mt-0.5">{desc}</div>
        <div className={`text-xs mt-1 ${status.cls}`}>{status.text}</div>
      </div>
      <button
        onClick={onRun}
        disabled={isRunning}
        className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40 shrink-0"
      >
        {buttonLabel(info, submitting)}
      </button>
    </div>
  );
}
