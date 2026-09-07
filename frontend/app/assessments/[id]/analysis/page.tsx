"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, describeError } from "@/lib/api";
import { use, useEffect, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { AiActivity, AiGlyph } from "@/components/AiActivity";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
import { compareScenariosByRisk } from "@/lib/utils";
import { staleLine, useWorkflow, type StepView } from "@/lib/useWorkflow";
import { waitForTask } from "@/lib/useTask";
import type { PhaseInfo, WorkflowKey } from "@/lib/types";
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

  // Live phase status + per-step gating from the backend's ready/blocked_by/
  // stale fields (same assessment query AssessmentShell uses; polls while a
  // job runs so the page stays live even if the user reloaded mid-run).
  const { assessment, step } = useWorkflow(aid);

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

  // A refused submit (409: prerequisite missing, stale, or another job in
  // flight) is shown on the step; the assessment is refetched so the gate
  // reflects the server's current view.
  const synthesize = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.synthesizeWeaknesses(aid);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await waitForTask(qc, task_id);
    },
    onSettled: invalidate,
  });

  const runGap = useMutation({
    mutationFn: async (onlyFailed: boolean = false) => {
      const { task_id } = await api.runGapAnalysis(aid, onlyFailed);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await waitForTask(qc, task_id);
      await api.recalculate(aid);
    },
    onSettled: invalidate,
  });

  // Cancel whichever job a step is running; the step flips to error
  // ("cancelled by user") and can be re-run — no 409 lingers behind it.
  const cancel = useMutation({
    mutationFn: (taskId: string) => api.cancelTask(taskId),
    onSettled: invalidate,
  });

  const writeNarratives = useMutation({
    mutationFn: async () => {
      await api.recalculate(aid);
      const { task_id } = await api.runNarratives(aid);
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
      await waitForTask(qc, task_id);
    },
    onSuccess: () => {
      router.push(`/assessments/${aid}/score`);
    },
    onSettled: invalidate,
  });

  // Auto-attach to running tasks discovered via phase_state on first render —
  // means a refresh mid-run still refetches once the task lands.
  useEffect(() => {
    const tasksToWatch = (["correlation", "analysis", "score"] as WorkflowKey[])
      .map((k) => assessment?.phases?.[k])
      .filter((p): p is PhaseInfo => !!p && p.state === "running" && !!p.task_id)
      .map((p) => p.task_id as string);
    if (tasksToWatch.length === 0) return;
    let cancelled = false;
    Promise.all(
      tasksToWatch.map((tid) => waitForTask(qc, tid).catch(() => undefined)),
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

  const synth = step("correlation");
  const gap = step("analysis");
  const narr = step("score");
  const gapInfo = gap.info;
  // "Re-run failed only" cannot address a stale gap analysis unless the only
  // change was adding controls (the backend refuses it otherwise).
  const resumeAllowed = !gap.stale || gap.stale.resume_ok;

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Analysis</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Three steps, in order: extracted findings are correlated across documents and mapped onto the scenarios,
        then every expected control is assessed against the evidence, then the narratives are written.
        Each step needs the previous one to be complete and current; every claim must be cited.
      </p>

      <div className="rounded-lg border border-ink-200 bg-white p-5 max-w-3xl space-y-4">
        <Step
          title="1. Cross-correlate weaknesses"
          desc="Maps extracted findings onto scenario controls and spawns emergent scenarios for risks the original scoping missed."
          kind="cross_correlation"
          view={synth}
          submitting={synthesize.isPending}
          onRun={() => synthesize.mutate()}
          onCancel={(tid) => cancel.mutate(tid)}
          cancelling={cancel.isPending}
          error={synthesize.isError ? describeError(synthesize.error) : undefined}
        />
        <div className="border-t border-ink-100" />
        <Step
          title="2. Run gap analysis"
          desc="Reasoner walks every scenario × expected control (including emergent scenarios from step 1) and pulls evidence from your uploaded documents."
          kind="gap_analysis"
          view={gap}
          submitting={runGap.isPending}
          onRun={() => runGap.mutate(false)}
          onCancel={(tid) => cancel.mutate(tid)}
          cancelling={cancel.isPending}
          warning={gapInfo?.warning || undefined}
          failedTargets={resumeAllowed ? gapInfo?.failed_targets : undefined}
          onRunFailed={() => runGap.mutate(true)}
          error={runGap.isError ? describeError(runGap.error) : undefined}
        />
        <div className="border-t border-ink-100" />
        <Step
          title="3. Generate narratives & summary, then continue"
          desc="Recalculates residual risk, writes the score-explanation prose per scenario and the executive summary."
          kind="narratives"
          view={narr}
          submitting={writeNarratives.isPending}
          onRun={() => writeNarratives.mutate()}
          onCancel={(tid) => cancel.mutate(tid)}
          cancelling={cancel.isPending}
          // A partial gap analysis (some controls failed) still satisfies this
          // step; repeat the warning so the assessor knows what the narratives
          // will be missing.
          warning={narr.canRun && gapInfo?.warning ? `Gap analysis: ${gapInfo.warning}` : undefined}
          error={writeNarratives.isError ? describeError(writeNarratives.error) : undefined}
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
      return <AiGlyph size={20} />;
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

// The non-running states; a running step renders <AiActivity> instead.
function statusLine(info: PhaseInfo | undefined): { text: string; cls: string } {
  if (!info || info.state === "pending") return { text: "Not run yet", cls: "text-ink-500" };
  if (info.state === "error") {
    return { text: info.error || "Failed — try again", cls: "text-risk-high" };
  }
  // done
  return {
    text: info.completed_at ? `Completed ${new Date(info.completed_at).toLocaleString()}` : "Completed",
    cls: "text-ink-500",
  };
}

function buttonLabel(info: PhaseInfo | undefined, submitting: boolean, stale: boolean): string {
  if (submitting) return "Running…";
  if (info?.state === "running") return "Running…";
  if (info?.state === "done") return stale ? "Re-run (stale)" : "Re-run";
  if (info?.state === "error") return "Retry";
  return "Run";
}

function Step({
  title, desc, kind, view, submitting, onRun, warning, failedTargets, onRunFailed, error, onCancel, cancelling,
}: {
  title: string;
  desc: string;
  // Task kind (backend app.workflow.KIND_*) — picks the indicator's verbs.
  kind: string;
  view: StepView;
  submitting: boolean;
  onRun: () => void;
  warning?: string;
  failedTargets?: string[];
  onRunFailed?: () => void;
  error?: string;
  onCancel?: (taskId: string) => void;
  cancelling?: boolean;
}) {
  const info = view.info;
  const status = statusLine(info);
  const isRunning = submitting || view.isRunning;
  const failed = failedTargets || [];
  const stale = staleLine(view.stale);
  // The backend is the authority; `canRun` mirrors its `ready` flag and the
  // reason is the first blocker it reported.
  const disabled = isRunning || !view.canRun;
  const disabledReason = !isRunning && !view.canRun ? view.blockedReason : null;
  return (
    <div className="flex items-start gap-3">
      <div className="pt-0.5">
        <StatusIcon state={info?.state} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <div className="text-sm font-semibold text-ink-900">{title}</div>
          {stale && info?.state === "done" && (
            <span className="rounded bg-amber-100 text-amber-800 text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5">
              stale
            </span>
          )}
        </div>
        <div className="text-xs text-ink-600 mt-0.5">{desc}</div>
        {isRunning ? (
          <AiActivity
            className="mt-2"
            variant="inline"
            kind={kind}
            source={info?.state === "running" ? info : undefined}
            onCancel={info?.state === "running" && info.task_id && onCancel ? () => onCancel(info.task_id as string) : undefined}
            cancelling={cancelling}
          />
        ) : (
          <div className={`text-xs mt-1 ${status.cls}`}>{status.text}</div>
        )}
        {stale && info?.state === "done" && (
          <div className="text-xs mt-0.5 text-amber-800">{stale}</div>
        )}
        {disabledReason && (
          <div className="text-xs mt-0.5 text-ink-500 italic">{disabledReason}</div>
        )}
        {error && (
          <div className="mt-1.5 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-700">{error}</div>
        )}
        {warning && (
          <div className="mt-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
            <div>{warning}</div>
            {failed.length > 0 && onRunFailed && (
              <button
                onClick={onRunFailed}
                disabled={disabled}
                title={disabledReason ?? undefined}
                className="mt-1 rounded border border-amber-300 bg-white text-amber-900 text-[11px] font-medium px-2 py-0.5 hover:bg-amber-100 disabled:opacity-40"
              >
                Re-run the {failed.length} failed control{failed.length === 1 ? "" : "s"} only
              </button>
            )}
          </div>
        )}
      </div>
      <button
        onClick={onRun}
        disabled={disabled}
        title={disabledReason ?? undefined}
        className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40 shrink-0"
      >
        {buttonLabel(info, submitting, !!stale)}
      </button>
    </div>
  );
}
