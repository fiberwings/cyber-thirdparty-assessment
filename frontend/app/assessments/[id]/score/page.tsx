"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScoreHeatmap } from "@/components/ScoreHeatmap";
import { ScoreExplanation } from "@/components/ScoreExplanation";
import { bandColor, bandLabel } from "@/lib/utils";
import clsx from "clsx";

export default function ScorePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);

  const { data: report, isLoading } = useQuery({
    queryKey: ["report", aid],
    queryFn: () => api.report(aid),
  });

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Residual risk</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Aggregates every scenario into a single overall band, alongside per-scenario residuals and the controls that
        drove the result. All scores are deterministic — edit anything in the scenario drawer and they recalculate
        instantly.
      </p>

      {isLoading || !report ? (
        <div className="text-sm text-ink-500">Loading…</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-[360px_1fr] gap-6">
          <div className="space-y-4">
            <div className={clsx("rounded-lg p-5 text-white", bandColor(report.aggregate.band))}>
              <div className="text-xs uppercase tracking-wide opacity-80">Overall residual risk</div>
              <div className="mt-1 text-3xl font-bold">{bandLabel(report.aggregate.band)}</div>
              <div className="mt-3 text-xs opacity-90 space-y-0.5">
                <div>Top-2 mean rank: {report.aggregate.top2_mean_rank}</div>
                <div>Weighted mean rank: {report.aggregate.weighted_mean_rank}</div>
              </div>
            </div>

            <ScoreHeatmap scenarios={report.scenarios} />

            <div className="rounded-lg border border-ink-200 bg-white p-4">
              <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-2">Weaknesses</div>
              {report.weaknesses.length === 0 ? (
                <div className="text-xs text-ink-500">None recorded yet.</div>
              ) : (
                <ul className="space-y-2 text-xs text-ink-700">
                  {report.weaknesses.map((w) => (
                    <li key={w.id} className="border-l-2 pl-2 border-rose-300">
                      <span className="text-[10px] uppercase tracking-wide font-bold mr-1 text-rose-700">{w.severity}</span>
                      {w.description}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <ScoreExplanation report={report} />
        </div>
      )}
    </AssessmentShell>
  );
}
