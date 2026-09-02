"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, describeError, pollTask } from "@/lib/api";
import { ExecutiveSummaryRead } from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { useWorkflow } from "@/lib/useWorkflow";
import clsx from "clsx";

const PRIORITY_STYLES: Record<string, { label: string; badge: string }> = {
  immediate: { label: "Immediate", badge: "bg-risk-high text-white" },
  near_term: { label: "Near term", badge: "bg-amber-100 text-amber-800" },
  monitor: { label: "Monitor", badge: "bg-ink-100 text-ink-700" },
};

export function ExecutiveSummaryPanel({
  assessmentId,
  summary,
  onScenarioClick,
}: {
  assessmentId: number;
  summary: ExecutiveSummaryRead | null;
  onScenarioClick?: (code: string) => void;
}) {
  const qc = useQueryClient();
  // The summary synthesises the scored state: it needs gap analysis done and
  // current (same prerequisite as narratives) and no other job running.
  const { step, anyRunning } = useWorkflow(assessmentId);
  const score = step("score");
  const canGenerate = score.info?.ready === true && !anyRunning;
  const blockedReason = !canGenerate ? (anyRunning ? "A job is running — wait for it to finish." : score.blockedReason) : null;
  const regenerate = useMutation({
    mutationFn: async () => {
      const { task_id } = await api.runExecutiveSummary(assessmentId);
      await pollTask(task_id, undefined, 800);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["report", assessmentId] });
      qc.invalidateQueries({ queryKey: ["assessment", assessmentId] });
    },
  });

  if (!summary) {
    return (
      <div className="rounded-xl border border-ink-200 bg-white p-5 shadow-card">
        <PanelTitle />
        <p className="mt-2 text-sm text-ink-600">
          No executive summary yet. It is written automatically at the end of the narratives step, or
          you can generate it now.
        </p>
        <button
          onClick={() => regenerate.mutate()}
          disabled={regenerate.isPending || !canGenerate}
          title={blockedReason ?? undefined}
          className="mt-3 rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40"
        >
          {regenerate.isPending ? "Generating…" : "Generate summary"}
        </button>
        {blockedReason && !regenerate.isPending && (
          <div className="mt-1.5 text-xs text-ink-500 italic">{blockedReason}</div>
        )}
        {regenerate.isError && (
          <div className="mt-2 text-xs text-risk-high">{describeError(regenerate.error)}</div>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-ink-200 bg-white shadow-card">
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <PanelTitle />
          <div className="text-[10px] text-ink-500 text-right shrink-0">
            {summary.generated_at ? `written ${relativeTime(summary.generated_at)}` : ""}
            {summary.model_id && <div>{summary.model_id}</div>}
          </div>
        </div>

        {summary.stale && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <span>
              Scores or findings changed after this summary was written — it may no longer reflect
              the assessment.
            </span>
            <button
              onClick={() => regenerate.mutate()}
              disabled={regenerate.isPending || !canGenerate}
              title={blockedReason ?? undefined}
              className="rounded bg-amber-600 text-white font-medium px-2.5 py-1 hover:bg-amber-700 disabled:opacity-40 shrink-0"
            >
              {regenerate.isPending ? "Regenerating…" : "Regenerate"}
            </button>
          </div>
        )}
        {regenerate.isError && (
          <div className="mt-2 text-xs text-risk-high">{describeError(regenerate.error)}</div>
        )}

        <p className="mt-3 text-sm text-ink-800 leading-relaxed">{summary.verdict}</p>
      </div>

      <div className="border-t border-ink-100 p-5">
        <SectionLabel>Key risks (most important first)</SectionLabel>
        <ol className="mt-2 space-y-3">
          {summary.key_risks.map((risk, i) => (
            <li key={i} className="rounded-lg border border-ink-100 bg-ink-50/50 p-3">
              <div className="flex items-baseline gap-2">
                <span className="text-xs font-bold text-ink-400 tabular-nums">{i + 1}.</span>
                <span className="text-sm font-semibold text-ink-900">{risk.title}</span>
              </div>
              <p className="mt-1 text-xs text-ink-700">{risk.why_it_matters}</p>
              {risk.evidence_basis && (
                <p className="mt-1 text-[11px] text-ink-500 italic">{risk.evidence_basis}</p>
              )}
              {(risk.scenario_codes.length > 0 || risk.weakness_ids.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {risk.scenario_codes.map((code) => (
                    <button
                      key={code}
                      onClick={() => onScenarioClick?.(code)}
                      className={clsx(
                        "rounded bg-white border border-ink-200 px-1.5 py-0.5 text-[10px] font-mono text-ink-700",
                        onScenarioClick && "hover:border-ink-400 cursor-pointer",
                      )}
                    >
                      {code}
                    </button>
                  ))}
                  {risk.weakness_ids.map((wid) => (
                    <span
                      key={wid}
                      className="rounded bg-white border border-ink-200 px-1.5 py-0.5 text-[10px] font-mono text-ink-500"
                    >
                      W-{wid}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ol>
      </div>

      {summary.recommended_actions.length > 0 && (
        <div className="border-t border-ink-100 p-5">
          <SectionLabel>Recommended actions</SectionLabel>
          <ul className="mt-2 space-y-1.5">
            {summary.recommended_actions.map((a, i) => {
              const p = PRIORITY_STYLES[a.priority] ?? PRIORITY_STYLES.monitor;
              return (
                <li key={i} className="flex items-start gap-2 text-xs text-ink-700">
                  <span className={clsx("rounded px-1.5 py-0.5 text-[10px] font-semibold shrink-0", p.badge)}>
                    {p.label}
                  </span>
                  <span className="min-w-0">
                    {a.action}
                    {a.related_scenario_codes.length > 0 && (
                      <span className="text-ink-400 font-mono text-[10px]">
                        {" "}({a.related_scenario_codes.join(", ")})
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {summary.limitations.length > 0 && (
        <div className="border-t border-ink-100 p-5">
          <SectionLabel>What this assessment could not establish</SectionLabel>
          <ul className="mt-2 list-disc pl-4 space-y-1 text-xs text-ink-600">
            {summary.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function PanelTitle() {
  return (
    <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
      Executive summary
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-wider text-ink-500 font-semibold">{children}</div>
  );
}
