"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, describeError } from "@/lib/api";
import { use, useEffect, useRef, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { AiActivity } from "@/components/AiActivity";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
import { compareScenariosByRisk } from "@/lib/utils";
import { staleLine, useWorkflow } from "@/lib/useWorkflow";
import { useTask } from "@/lib/useTask";
import { useRouter } from "next/navigation";

export default function ScenariosPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const router = useRouter();
  const qc = useQueryClient();

  const { data: scenarios, isLoading } = useQuery({
    queryKey: ["scenarios", aid],
    queryFn: () => api.listScenarios(aid),
  });
  // Same key as AssessmentShell — shares its cache entry. `step` carries the
  // backend's gating (scoping must be done; no other job running).
  const { assessment, step } = useWorkflow(aid);
  const scenariosStep = step("scenarios");

  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // Tasks this mount already polled to completion — the cached phase data can
  // briefly still say "running" for them until the refetch lands.
  const finishedTasks = useRef<Set<string>>(new Set());

  // Re-attach to a generation already in flight (page was navigated away from
  // and back, or opened in a new tab): the backend persists the running task
  // id in the phase state, so recover it and resume polling.
  const phase = assessment?.phases?.scenarios;
  useEffect(() => {
    if (phase?.state === "running" && phase.task_id && !taskId && !finishedTasks.current.has(phase.task_id)) {
      setTaskId(phase.task_id);
    }
  }, [phase?.state, phase?.task_id, taskId]);

  // One polled query driven by taskId (lib/useTask); survives whichever way
  // the id arrived (button click or recovery above). Cancelling ends it with
  // the backend's "cancelled by user" error and the step becomes re-runnable.
  const { task, cancel: cancelGeneration } = useTask(taskId);
  useEffect(() => {
    if (!taskId || !task || (task.status !== "done" && task.status !== "error")) return;
    finishedTasks.current.add(taskId);
    if (task.status === "error") setTaskError(task.detail || task.error || "Generation failed");
    setTaskId(null);
    qc.invalidateQueries({ queryKey: ["scenarios", aid] });
    qc.invalidateQueries({ queryKey: ["assessment", aid] });
  }, [taskId, task, aid, qc]);

  const generate = useMutation({
    // The backend re-attaches to an in-flight run instead of starting a second
    // one, so this is safe even if a stray click gets through.
    mutationFn: () => api.generateScenarios(aid),
    onSuccess: ({ task_id }) => {
      setTaskError(null);
      setTaskId(task_id);
    },
    onError: (e) => {
      setTaskError(describeError(e));
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
    },
  });

  const generating = generate.isPending || taskId !== null;
  const blockedReason = !generating && !scenariosStep.canRun ? scenariosStep.blockedReason : null;
  const stale = staleLine(scenariosStep.stale);

  const sortedScenarios = scenarios ? [...scenarios].sort(compareScenariosByRisk) : undefined;
  const selected = scenarios?.find((s) => s.id === selectedId) || null;

  return (
    <AssessmentShell id={aid}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold">Inherent risk scenarios</h2>
          <p className="text-sm text-ink-600 mt-1 max-w-2xl">
            The AI proposes scenarios from the service description and assigns inherent impact and likelihood.
            Click any card to edit and to inspect expected controls.
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            onClick={() => generate.mutate()}
            disabled={generating || !scenariosStep.canRun}
            title={blockedReason ?? undefined}
            className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
          >
            {generating ? "Generating…" : (scenarios?.length ? (stale ? "Regenerate (stale)" : "Regenerate") : "Generate scenarios")}
          </button>
          {scenarios && scenarios.length > 0 && (
            <button
              onClick={() => router.push(`/assessments/${aid}/evidence`)}
              className="rounded border border-ink-300 text-ink-700 text-sm font-medium px-4 py-2 hover:bg-ink-50"
            >
              Upload evidence →
            </button>
          )}
        </div>
      </div>

      {blockedReason && (
        <div className="mt-3 text-xs text-ink-500 italic text-right">{blockedReason}</div>
      )}
      {stale && scenariosStep.info?.state === "done" && (
        <div className="mt-4 rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
          {stale}. Regenerate the scenarios before continuing — every downstream step is blocked
          until this step is current.
        </div>
      )}
      {taskId && (
        <AiActivity
          className="mt-4"
          variant="panel"
          kind="scenarios_generation"
          source={task ?? (phase?.state === "running" ? phase : undefined)}
          onCancel={() => cancelGeneration.mutate(taskId)}
          cancelling={cancelGeneration.isPending}
          hint="You can leave this page — generation continues and progress reappears when you come back."
        />
      )}
      {taskError && (
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          Generation failed: {taskError}
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {isLoading ? (
          <div className="text-sm text-ink-500 col-span-full">Loading…</div>
        ) : sortedScenarios && sortedScenarios.length > 0 ? (
          sortedScenarios.map((s) => (
            <ScenarioCard key={s.id} scenario={s} onOpen={() => setSelectedId(s.id)} />
          ))
        ) : (
          <div className="rounded-lg border border-dashed border-ink-200 bg-white p-8 text-center text-sm text-ink-500 col-span-full">
            No scenarios yet. Click <strong>Generate scenarios</strong> to start.
          </div>
        )}
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
