"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import {
  bandColor,
  bandLabel,
  compareScenarioScoresByRisk,
  compareWeaknessesBySeverity,
  formatPercent,
} from "@/lib/utils";
import clsx from "clsx";

export default function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const { data: report } = useQuery({
    queryKey: ["report", aid],
    queryFn: () => api.report(aid),
  });

  return (
    <AssessmentShell id={aid}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold">Final report</h2>
          <p className="text-sm text-ink-600 mt-1 max-w-2xl">
            A printable summary suitable for risk-acceptance sign-off.
          </p>
        </div>
        <button
          onClick={() => window.print()}
          className="rounded border border-ink-300 text-ink-700 text-sm font-medium px-4 py-2 hover:bg-ink-50"
        >
          Print
        </button>
      </div>

      {!report ? (
        <div className="mt-6 text-sm text-ink-500">Loading…</div>
      ) : (
        <div className="mt-6 max-w-4xl space-y-6">
          <Section title="Vendor & overall verdict">
            <div className="flex items-center justify-between rounded-lg border border-ink-200 bg-white p-5">
              <div>
                <div className="text-xs uppercase tracking-wide text-ink-500">Vendor</div>
                <div className="text-lg font-semibold">{report.assessment.vendor_name}</div>
              </div>
              <div className={clsx("rounded-md text-white text-sm font-bold px-4 py-2", bandColor(report.aggregate.band))}>
                {bandLabel(report.aggregate.band)}
              </div>
            </div>
          </Section>

          <Section title="Service description">
            <div className="rounded-lg border border-ink-200 bg-white p-5 text-sm whitespace-pre-wrap">
              {report.description?.text || "—"}
            </div>
          </Section>

          <Section title={`Documents reviewed (${report.documents.length})`}>
            <ul className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
              {report.documents.map((d) => (
                <li key={d.id} className="px-4 py-2 text-sm flex items-center justify-between">
                  <span>{d.filename}</span>
                  <span className="text-xs uppercase tracking-wide text-ink-500">{d.kind}</span>
                </li>
              ))}
              {report.documents.length === 0 && (
                <li className="px-4 py-3 text-sm text-ink-500">No documents.</li>
              )}
            </ul>
          </Section>

          <Section title={`Scenarios (${report.scenarios.length})`}>
            <ul className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
              {[...report.scenarios].sort(compareScenarioScoresByRisk).map((s) => (
                <li key={s.code} className="px-4 py-3">
                  <div className="flex items-center justify-between">
                    <div className="font-medium text-sm text-ink-900">{s.name}</div>
                    <div className={clsx("rounded-md text-white text-[11px] font-semibold px-2 py-1", bandColor(s.band))}>
                      {bandLabel(s.band)}
                    </div>
                  </div>
                  <div className="mt-1 text-[11px] text-ink-500">
                    Inherent {s.inherent_impact}/{s.inherent_likelihood} → Residual {s.residual_impact}/{s.residual_likelihood}
                    · Coverage {formatPercent(s.coverage_index)} · Reduction -{s.likelihood_reduction} · Uplift +{s.meta_uplift}
                  </div>
                  {s.rationale && (
                    <div className="mt-2 text-xs text-ink-700 whitespace-pre-wrap">{s.rationale}</div>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          <Section title={`Weaknesses (${report.weaknesses.length})`}>
            <ul className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
              {[...report.weaknesses].sort(compareWeaknessesBySeverity).map((w) => (
                <li key={w.id} className="px-4 py-3 text-sm">
                  <span className={clsx(
                    "text-[10px] uppercase tracking-wide font-bold mr-2 rounded px-1.5 py-0.5",
                    w.severity === "critical" ? "bg-rose-700 text-white"
                      : w.severity === "high" ? "bg-rose-100 text-rose-800"
                      : w.severity === "medium" ? "bg-amber-100 text-amber-800"
                      : "bg-ink-100 text-ink-700"
                  )}>
                    {w.severity}
                  </span>
                  {w.description}
                  {w.quote && <div className="mt-1 text-xs text-ink-500 italic">&ldquo;{w.quote}&rdquo;</div>}
                </li>
              ))}
              {report.weaknesses.length === 0 && (
                <li className="px-4 py-3 text-sm text-ink-500">None.</li>
              )}
            </ul>
          </Section>
        </div>
      )}
    </AssessmentShell>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-2">{title}</div>
      {children}
    </section>
  );
}
