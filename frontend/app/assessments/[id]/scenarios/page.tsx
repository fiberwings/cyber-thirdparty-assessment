"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, pollTask } from "@/lib/api";
import { use, useEffect, useRef, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
import { compareScenariosByRisk } from "@/lib/utils";
import { useRouter } from "next/navigation";

type Progress = { status: string; progress: number; detail: string };

export default function ScenariosPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const router = useRouter();
  const qc = useQueryClient();

  const { data: scenarios, isLoading } = useQuery({
    queryKey: ["scenarios", aid],
    queryFn: () => api.listScenarios(aid),
  });
  // Same key as AssessmentShell — shares its cache entry.
  const { data: assessment } = useQuery({
    queryKey: ["assessment", aid],
    queryFn: () => api.getAssessment(aid),
  });

  const [progress, setProgress] = useState<Progress | null>(null);
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
      setProgress({
        status: "running",
        progress: phase.progress ?? 0,
        detail: phase.detail ?? "",
      });
    }
  }, [phase?.state, phase?.task_id, phase?.progress, phase?.detail, taskId]);

  // Single polling loop driven by taskId; survives whichever way the task id
  // arrived (button click or recovery above).
  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    (async () => {
      try {
        await pollTask(taskId, (p) => {
          if (!cancelled) setProgress(p);
        });
        if (cancelled) return;
        setProgress(null);
      } catch (e) {
        if (cancelled) return;
        setProgress(null);
        setTaskError(e instanceof Error ? e.message : String(e));
      }
      finishedTasks.current.add(taskId);
      setTaskId(null);
      qc.invalidateQueries({ queryKey: ["scenarios", aid] });
      qc.invalidateQueries({ queryKey: ["assessment", aid] });
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId, aid, qc]);

  const generate = useMutation({
    // The backend re-attaches to an in-flight run instead of starting a second
    // one, so this is safe even if a stray click gets through.
    mutationFn: () => api.generateScenarios(aid),
    onSuccess: ({ task_id }) => {
      setTaskError(null);
      setTaskId(task_id);
    },
    onError: (e) => setTaskError(e instanceof Error ? e.message : String(e)),
  });

  const generating = generate.isPending || taskId !== null;

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
            disabled={generating}
            className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
          >
            {generating ? "Generating…" : (scenarios?.length ? "Regenerate" : "Generate scenarios")}
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

      {progress && (
        <div className="mt-4 rounded border border-ink-200 bg-white p-3 text-xs text-ink-600">
          {progress.status} · {Math.round(progress.progress * 100)}% · {progress.detail || "…"}
          <span className="block mt-1 text-ink-400">
            The first step (scenario skeletons) typically takes a few minutes. You can leave this
            page — generation continues and progress reappears when you come back.
          </span>
        </div>
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
