"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, pollTask } from "@/lib/api";
import { use, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
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

  const [progress, setProgress] = useState<{ status: string; progress: number; detail: string } | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const runGap = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.runGapAnalysis(aid);
      await pollTask(task_id, setProgress, 800);
      await api.recalculate(aid);
    },
    onSuccess: () => {
      setProgress(null);
      qc.invalidateQueries({ queryKey: ["scenarios", aid] });
      qc.invalidateQueries({ queryKey: ["report", aid] });
    },
  });

  const synthesize = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.synthesizeWeaknesses(aid);
      await pollTask(task_id, setProgress, 800);
    },
    onSuccess: () => {
      setProgress(null);
      qc.invalidateQueries({ queryKey: ["scenarios", aid] });
      qc.invalidateQueries({ queryKey: ["weaknesses", aid] });
    },
  });

  const writeNarratives = useMutation({
    mutationFn: async () => {
      await api.recalculate(aid);
      const { task_id } = await api.runNarratives(aid);
      await pollTask(task_id, setProgress, 800);
    },
    onSuccess: () => {
      setProgress(null);
      qc.invalidateQueries({ queryKey: ["scenarios", aid] });
      router.push(`/assessments/${aid}/score`);
    },
  });

  const selected = scenarios?.find((s) => s.id === selectedId) || null;

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Gap analysis</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        For each expected control, the reasoner retrieves candidate evidence and decides coverage and effectiveness.
        Every claim must be cited.
      </p>

      <div className="rounded-lg border border-ink-200 bg-white p-5 max-w-3xl space-y-3">
        <Step
          title="1. Run gap analysis"
          desc="Reasoner walks every scenario × expected control and pulls evidence from your uploaded documents."
          buttonLabel="Run gap analysis"
          loading={runGap.isPending}
          onRun={() => runGap.mutate()}
        />
        <Step
          title="2. Synthesize weaknesses & emergent scenarios"
          desc="Looks for findings the original scoping missed (e.g., pen-test highs surfacing a new attack path)."
          buttonLabel="Run synthesis"
          loading={synthesize.isPending}
          onRun={() => synthesize.mutate()}
        />
        <Step
          title="3. Generate narratives & continue"
          desc="Recalculates residual risk and writes the score-explanation prose for each scenario."
          buttonLabel="Generate & view scores"
          loading={writeNarratives.isPending}
          onRun={() => writeNarratives.mutate()}
        />
        {progress && (
          <div className="rounded border border-ink-200 bg-ink-50 p-3 text-xs text-ink-700">
            {progress.status} · {Math.round(progress.progress * 100)}% · {progress.detail || "…"}
          </div>
        )}
      </div>

      <div className="mt-8">
        <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Per-scenario results</div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {scenarios?.map((s) => (
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

function Step({
  title, desc, buttonLabel, loading, onRun,
}: { title: string; desc: string; buttonLabel: string; loading: boolean; onRun: () => void }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div>
        <div className="text-sm font-semibold text-ink-900">{title}</div>
        <div className="text-xs text-ink-600 mt-0.5">{desc}</div>
      </div>
      <button
        onClick={onRun}
        disabled={loading}
        className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40 shrink-0"
      >
        {loading ? "Running…" : buttonLabel}
      </button>
    </div>
  );
}
