"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScoreHeatmap } from "@/components/ScoreHeatmap";
import { ScoreExplanation } from "@/components/ScoreExplanation";
import { bandColor, bandLabel, bandTextColor, compareWeaknessesBySeverity } from "@/lib/utils";
import { Band, WeaknessRead } from "@/lib/types";
import clsx from "clsx";

const SEVERITY_STYLES: Record<WeaknessRead["severity"], { dot: string; label: string }> = {
  critical: { dot: "bg-risk-veryhigh", label: "text-risk-veryhigh" },
  high:     { dot: "bg-risk-high",     label: "text-risk-high" },
  medium:   { dot: "bg-risk-moderate", label: "text-amber-700" },
  low:      { dot: "bg-risk-low",      label: "text-risk-low" },
};

export default function ScorePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);

  const { data: report, isLoading } = useQuery({
    queryKey: ["report", aid],
    queryFn: () => api.report(aid),
  });

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold text-ink-900 mb-1">Residual risk</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Aggregates every scenario into a single overall band, alongside per-scenario residuals and the controls that
        drove the result. All scores are deterministic — edit anything in the scenario drawer and they recalculate
        instantly.
      </p>

      {isLoading || !report ? (
        <div className="text-sm text-ink-500">Loading…</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-6">
          <div className="space-y-4">
            <OverallCard band={report.aggregate.band} top2={report.aggregate.top2_mean_rank} weighted={report.aggregate.weighted_mean_rank} />

            <ScoreHeatmap scenarios={report.scenarios} />

            <WeaknessCard weaknesses={report.weaknesses} />
          </div>

          <ScoreExplanation report={report} />
        </div>
      )}
    </AssessmentShell>
  );
}

function OverallCard({ band, top2, weighted }: { band: Band; top2: number; weighted: number }) {
  return (
    <div className="overflow-hidden rounded-xl border border-ink-200 bg-white shadow-card">
      <div className={clsx("h-1.5", bandColor(band))} />
      <div className="p-5">
        <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
          Overall residual risk
        </div>

        <div className="mt-3 flex items-center gap-3">
          <span className={clsx("h-3 w-3 rounded-full ring-4 ring-ink-100", bandColor(band))} aria-hidden />
          <span className={clsx("text-3xl font-bold tracking-tight", bandTextColor(band))}>
            {bandLabel(band)}
          </span>
        </div>

        <dl className="mt-5 grid grid-cols-2 gap-3 border-t border-ink-100 pt-4">
          <div>
            <dt className="text-[10px] uppercase tracking-wider text-ink-500 font-semibold">
              Top-2 mean rank
            </dt>
            <dd className="mt-0.5 text-lg font-semibold text-ink-900 tabular-nums">
              {top2.toFixed(2)}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase tracking-wider text-ink-500 font-semibold">
              Weighted mean
            </dt>
            <dd className="mt-0.5 text-lg font-semibold text-ink-900 tabular-nums">
              {weighted.toFixed(2)}
            </dd>
          </div>
        </dl>
      </div>
    </div>
  );
}

function WeaknessCard({ weaknesses }: { weaknesses: WeaknessRead[] }) {
  const sorted = [...weaknesses].sort(compareWeaknessesBySeverity);
  return (
    <div className="rounded-xl border border-ink-200 bg-white p-5 shadow-card">
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
          Weaknesses
        </div>
        {sorted.length > 0 && (
          <div className="text-[10px] text-ink-500">{sorted.length}</div>
        )}
      </div>
      {sorted.length === 0 ? (
        <div className="text-xs text-ink-500">None recorded yet.</div>
      ) : (
        <ul className="space-y-2.5 text-xs text-ink-700">
          {sorted.map((w) => {
            const sev = SEVERITY_STYLES[w.severity];
            return (
              <li key={w.id} className="flex gap-2">
                <span className={clsx("mt-1 h-1.5 w-1.5 rounded-full shrink-0", sev.dot)} aria-hidden />
                <div className="min-w-0">
                  <span className={clsx("text-[10px] uppercase tracking-wider font-bold mr-1.5", sev.label)}>
                    {w.severity}
                  </span>
                  <span className="text-ink-700">{w.description}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
