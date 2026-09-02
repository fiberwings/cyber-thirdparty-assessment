"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, describeError, pollTask } from "@/lib/api";
import { use, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { AssessmentSettings } from "@/components/AssessmentSettings";
import { TaskProgress } from "@/components/TaskProgress";
import { useRouter } from "next/navigation";

const DIMENSIONS = [
  "data_types", "hosting", "network_access", "identity_flow",
  "regulatory_scope", "geography", "criticality",
];

export default function ScopingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const router = useRouter();
  const qc = useQueryClient();

  const { data: desc } = useQuery({
    queryKey: ["description", aid],
    queryFn: () => api.getDescription(aid),
  });

  const [text, setText] = useState("");
  const [answer, setAnswer] = useState("");
  const [progress, setProgress] = useState<{ status: string; progress: number; detail: string } | null>(null);

  // Description / scoping changes reopen scoping and stamp every later step
  // stale (and are refused while a job runs), so the phases are refreshed too.
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["description", aid] });
    qc.invalidateQueries({ queryKey: ["assessment", aid] });
  };
  const setDescription = useMutation({
    mutationFn: (text: string) => api.setDescription(aid, text),
    onSettled: refresh,
  });
  const turn = useMutation({
    mutationFn: async (answer?: string) => {
      const { task_id } = await api.scopingTurn(aid, answer);
      await pollTask(task_id, setProgress, 800);
    },
    onSuccess: () => setAnswer(""),
    onSettled: () => {
      setProgress(null);
      refresh();
    },
  });
  const force = useMutation({
    mutationFn: () => api.forceContinue(aid),
    onSuccess: () => router.push(`/assessments/${aid}/scenarios`),
    onSettled: refresh,
  });

  const hasDesc = !!desc && desc.text;

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Scope the service</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Describe what the vendor will provide. The AI will ask follow-up questions until enough is known to identify
        inherent risks — or you can force-continue at any time.
      </p>

      <AssessmentSettings assessmentId={aid} />

      {!hasDesc && (
        <div className="rounded-lg border border-ink-200 bg-white p-5 max-w-3xl">
          <label className="block text-xs uppercase tracking-wide text-ink-500 font-semibold mb-2">
            Initial description
          </label>
          <textarea
            className="w-full rounded border border-ink-200 px-3 py-2 text-sm min-h-[140px]"
            placeholder="What service does the vendor provide? What data do they handle? Where does it run?"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="flex justify-end mt-3">
            <button
              onClick={() => setDescription.mutate(text)}
              disabled={text.trim().length < 10 || setDescription.isPending}
              className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
            >
              {setDescription.isPending ? "Saving…" : "Submit description"}
            </button>
          </div>
          {setDescription.isError && (
            <div className="mt-2 text-xs text-risk-high">{describeError(setDescription.error)}</div>
          )}
        </div>
      )}

      {hasDesc && (
        <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-6">
          <div className="space-y-3">
            <div className="rounded-lg border border-ink-200 bg-white p-4">
              <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-1">Description</div>
              <div className="text-sm text-ink-800 whitespace-pre-wrap">{desc!.text}</div>
            </div>

            {desc!.turns.map((t) => (
              <div
                key={t.id}
                className={`rounded-lg p-3 border ${
                  t.role === "ai"
                    ? "bg-blue-50 border-blue-200"
                    : "bg-white border-ink-200"
                }`}
              >
                <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">
                  {t.role === "ai" ? "AI question" : "Your answer"}
                </div>
                <div className="text-sm text-ink-800 whitespace-pre-wrap">{t.content}</div>
              </div>
            ))}

            {!desc!.is_sufficient && (
              <div className="rounded-lg border border-ink-200 bg-white p-3">
                <textarea
                  className="w-full rounded border border-ink-200 px-3 py-2 text-sm min-h-[80px] disabled:opacity-40"
                  placeholder="Answer the AI's question (or press 'Get next question' to start)…"
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  disabled={turn.isPending}
                />
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    onClick={() => force.mutate()}
                    disabled={force.isPending || turn.isPending}
                    className="rounded border border-ink-300 text-ink-700 text-xs font-medium px-3 py-1.5 hover:bg-ink-50 disabled:opacity-40"
                  >
                    Force continue →
                  </button>
                  <button
                    onClick={() => turn.mutate(answer || undefined)}
                    disabled={turn.isPending}
                    className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40"
                  >
                    {turn.isPending ? "Asking…" : (desc!.turns.length === 0 ? "Start scoping" : "Submit answer")}
                  </button>
                </div>
                {turn.isPending && (
                  <div className="mt-2">
                    <TaskProgress label="Processing" detail={progress?.detail} />
                  </div>
                )}
                {turn.isError && (
                  <div className="mt-2 text-xs text-risk-high">{describeError(turn.error)}</div>
                )}
                {force.isError && (
                  <div className="mt-2 text-xs text-risk-high">{describeError(force.error)}</div>
                )}
              </div>
            )}

            {desc!.is_sufficient && (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4">
                <div className="text-sm font-semibold text-emerald-900">Scope is sufficient ✓</div>
                <p className="text-xs text-emerald-800 mt-1">
                  You can now generate inherent risk scenarios.
                </p>
                <button
                  onClick={() => router.push(`/assessments/${aid}/scenarios`)}
                  className="mt-3 rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700"
                >
                  Continue to scenarios →
                </button>
              </div>
            )}
          </div>

          <aside className="rounded-lg border border-ink-200 bg-white p-4 h-fit">
            <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Sufficiency breakdown</div>
            <div className="space-y-2">
              {DIMENSIONS.map((d) => {
                const v = (desc?.sufficiency_json?.[d] as number) || 0;
                return (
                  <div key={d}>
                    <div className="flex items-center justify-between text-[11px] mb-0.5">
                      <span className="text-ink-700 capitalize">{d.replace("_", " ")}</span>
                      <span className={v >= 3 ? "text-emerald-700" : "text-amber-700"}>{v}/5</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
                      <div
                        className={`h-full ${v >= 3 ? "bg-emerald-500" : "bg-amber-500"}`}
                        style={{ width: `${(v / 5) * 100}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </aside>
        </div>
      )}
    </AssessmentShell>
  );
}
