"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use, useMemo, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { ScoreHeatmap } from "@/components/ScoreHeatmap";
import { ScoreExplanation } from "@/components/ScoreExplanation";
import { ExecutiveSummaryPanel } from "@/components/ExecutiveSummaryPanel";
import {
  SEVERITY_ORDER,
  SEVERITY_STYLES,
  bandColor,
  bandLabel,
  bandTextColor,
  compareWeaknessesBySeverity,
  severityCounts,
} from "@/lib/utils";
import { Band, DocumentRead, WeaknessRead } from "@/lib/types";
import { SeverityBadge } from "@/components/SeverityBadge";
import clsx from "clsx";

export default function ScorePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const [highlightCode, setHighlightCode] = useState<string | null>(null);

  const { data: report, isLoading } = useQuery({
    queryKey: ["report", aid],
    queryFn: () => api.report(aid),
  });

  const scrollToScenario = (code: string) => {
    setHighlightCode(code);
    // Defer until the details element opens.
    requestAnimationFrame(() => {
      document.getElementById(`scenario-${code}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold text-ink-900 mb-1">Residual risk</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        The executive summary surfaces what matters most; the sections below carry the full detail.
        All scores are deterministic — edit anything in the scenario drawer and they recalculate instantly.
      </p>

      {isLoading || !report ? (
        <div className="text-sm text-ink-500">Loading…</div>
      ) : (
        <div className="max-w-5xl space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-6 items-start">
            <OverallCard
              band={report.aggregate.band}
              top2={report.aggregate.top2_mean_rank}
              weighted={report.aggregate.weighted_mean_rank}
            />
            <ScoreHeatmap scenarios={report.scenarios} />
          </div>

          <ExecutiveSummaryPanel
            assessmentId={aid}
            summary={report.executive_summary}
            onScenarioClick={scrollToScenario}
          />

          <WeaknessCard weaknesses={report.weaknesses} documents={report.documents} />

          <section>
            <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">
              Per-scenario detail
            </div>
            <ScoreExplanation report={report} highlightCode={highlightCode} />
          </section>
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

        <details className="mt-4 border-t border-ink-100 pt-3 group">
          <summary className="cursor-pointer select-none text-[11px] text-ink-500 hover:text-ink-700 list-none [&::-webkit-details-marker]:hidden">
            <span className="group-open:hidden">How the overall band is derived ▸</span>
            <span className="hidden group-open:inline">How the overall band is derived ▾</span>
          </summary>
          <dl className="mt-3 space-y-2 text-xs text-ink-700">
            <div className="flex items-baseline justify-between gap-2">
              <dt>Mean of the two worst scenario ranks</dt>
              <dd className="font-semibold tabular-nums">{top2.toFixed(2)}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-2">
              <dt>Impact-weighted mean rank</dt>
              <dd className="font-semibold tabular-nums">{weighted.toFixed(2)}</dd>
            </div>
            <p className="text-[11px] text-ink-500 pt-1">
              Scenario bands map to ranks 1 (Low) – 4 (Very High). The overall band is the more
              conservative of the two figures, rounded.
            </p>
          </dl>
        </details>
      </div>
    </div>
  );
}

function WeaknessCard({
  weaknesses,
  documents,
}: {
  weaknesses: WeaknessRead[];
  documents: DocumentRead[];
}) {
  const [showAll, setShowAll] = useState(false);
  const docById = useMemo(
    () => new Map(documents.map((d) => [d.id, d] as const)),
    [documents],
  );
  const sorted = useMemo(
    () => [...weaknesses].sort(compareWeaknessesBySeverity),
    [weaknesses],
  );
  const counts = severityCounts(sorted);
  const important = sorted.filter((w) => w.severity === "critical" || w.severity === "high");
  const rest = sorted.filter((w) => w.severity !== "critical" && w.severity !== "high");
  const visible = showAll ? sorted : important;

  return (
    <div className="rounded-xl border border-ink-200 bg-white p-5 shadow-card">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
          Weaknesses
        </div>
        <div className="flex items-center gap-1.5">
          {SEVERITY_ORDER.filter((s) => counts[s] > 0).map((s) => (
            <span
              key={s}
              className={clsx(
                "rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums",
                SEVERITY_STYLES[s].badge,
              )}
            >
              {counts[s]} {s}
            </span>
          ))}
          {sorted.length === 0 && <span className="text-[10px] text-ink-500">none</span>}
        </div>
      </div>

      {sorted.length === 0 ? (
        <div className="text-xs text-ink-500">None recorded yet.</div>
      ) : (
        <>
          {visible.length === 0 && (
            <div className="text-xs text-ink-500">
              No critical or high findings. {rest.length} lower-severity finding{rest.length === 1 ? "" : "s"} below.
            </div>
          )}
          <ul className="space-y-2.5 text-xs text-ink-700">
            {visible.map((w) => {
              const doc = w.source_document_id != null ? docById.get(w.source_document_id) : undefined;
              return (
                <li key={w.id} className="flex gap-2">
                  <SeverityBadge severity={w.severity} className="mt-0.5 mr-0.5 shrink-0" />
                  <span className="text-ink-700 min-w-0">
                    {w.description}
                    <span className="ml-1.5 text-[10px] text-ink-400">
                      {doc ? doc.filename : ""}
                      {w.unmatched && (
                        <span className="ml-1.5 rounded bg-ink-100 px-1 py-0.5 text-ink-500 uppercase tracking-wide">
                          not scored
                        </span>
                      )}
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
          {rest.length > 0 && (
            <button
              onClick={() => setShowAll((v) => !v)}
              className="mt-3 text-[11px] text-ink-500 hover:text-ink-800 underline underline-offset-2"
            >
              {showAll
                ? "Show critical & high only"
                : `Show ${rest.length} medium/low finding${rest.length === 1 ? "" : "s"}`}
            </button>
          )}
        </>
      )}
    </div>
  );
}
