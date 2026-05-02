"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, pollTask } from "@/lib/api";
import { use, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScenarioCard } from "@/components/ScenarioCard";
import { ScenarioDrawer } from "@/components/ScenarioDrawer";
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

  const [progress, setProgress] = useState<{ status: string; progress: number; detail: string } | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const generate = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.generateScenarios(aid);
      await pollTask(task_id, setProgress);
    },
    onSuccess: () => {
      setProgress(null);
      qc.invalidateQueries({ queryKey: ["scenarios", aid] });
    },
  });

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
            disabled={generate.isPending}
            className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
          >
            {generate.isPending ? "Generating…" : (scenarios?.length ? "Regenerate" : "Generate scenarios")}
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
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {isLoading ? (
          <div className="text-sm text-ink-500 col-span-full">Loading…</div>
        ) : scenarios && scenarios.length > 0 ? (
          scenarios.map((s) => (
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
