"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { PrerequisitesBanner } from "@/components/PrerequisitesBanner";
import {
  SEVERITY_ORDER,
  SEVERITY_STYLES,
  bandColor,
  bandLabel,
  compareScenarioScoresByRisk,
  compareWeaknessesBySeverity,
  formatPercent,
} from "@/lib/utils";
import { ScenarioScoreRead } from "@/lib/types";
import clsx from "clsx";

function driverLine(s: ScenarioScoreRead): string {
  const parts = [
    `Coverage ${formatPercent(s.coverage_index)} → −${s.likelihood_reduction} likelihood`,
  ];
  if (s.uplift > 0) parts.push(`uplift +${s.uplift}`);
  return parts.join(" · ");
}

export default function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const { data: report } = useQuery({
    queryKey: ["report", aid],
    queryFn: () => api.report(aid),
  });

  const summary = report?.executive_summary ?? null;

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

      <div className="mt-6 print:hidden">
        <PrerequisitesBanner assessmentId={aid} />
      </div>

      {!report ? (
        <div className="mt-6 text-sm text-ink-500">Loading…</div>
      ) : (
        <div className="mt-6 max-w-4xl space-y-6">
          <Section title="Vendor & overall verdict">
            <div className="rounded-lg border border-ink-200 bg-white p-5">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs uppercase tracking-wide text-ink-500">Vendor</div>
                  <div className="text-lg font-semibold">{report.assessment.vendor_name}</div>
                </div>
                <div className={clsx("rounded-md text-white text-sm font-bold px-4 py-2", bandColor(report.aggregate.band))}>
                  {bandLabel(report.aggregate.band)}
                </div>
              </div>
              {summary && (
                <p className="mt-4 text-sm text-ink-800 leading-relaxed border-t border-ink-100 pt-4">
                  {summary.verdict}
                </p>
              )}
            </div>
          </Section>

          {summary && (
            <>
              <Section title="Key risks">
                <ol className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
                  {summary.key_risks.map((risk, i) => (
                    <li key={i} className="px-4 py-3">
                      <div className="text-sm font-semibold text-ink-900">
                        {i + 1}. {risk.title}
                        {(risk.scenario_codes.length > 0 || risk.weakness_ids.length > 0) && (
                          <span className="ml-2 font-mono font-normal text-[10px] text-ink-400">
                            {[...risk.scenario_codes, ...risk.weakness_ids.map((w) => `W-${w}`)].join(", ")}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-ink-700">{risk.why_it_matters}</p>
                      {risk.evidence_basis && (
                        <p className="mt-1 text-[11px] text-ink-500 italic">{risk.evidence_basis}</p>
                      )}
                    </li>
                  ))}
                </ol>
              </Section>

              {summary.recommended_actions.length > 0 && (
                <Section title="Recommended actions">
                  <ul className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
                    {summary.recommended_actions.map((a, i) => (
                      <li key={i} className="px-4 py-2.5 text-sm flex items-start gap-2">
                        <span className="text-[10px] uppercase tracking-wide font-bold text-ink-500 mt-0.5 shrink-0 w-20">
                          {a.priority.replace("_", " ")}
                        </span>
                        <span>{a.action}</span>
                      </li>
                    ))}
                  </ul>
                </Section>
              )}

              {summary.limitations.length > 0 && (
                <Section title="Assessment limitations">
                  <ul className="rounded-lg border border-ink-200 bg-white px-4 py-3 list-disc pl-8 space-y-1 text-sm text-ink-700">
                    {summary.limitations.map((l, i) => (
                      <li key={i}>{l}</li>
                    ))}
                  </ul>
                </Section>
              )}
            </>
          )}

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
                    {" · "}{driverLine(s)}
                  </div>
                  {s.rationale && (
                    <div className="mt-2 text-xs text-ink-700 whitespace-pre-wrap">{s.rationale}</div>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          <Section title={`Weaknesses (${report.weaknesses.length})`}>
            <div className="rounded-lg border border-ink-200 bg-white">
              {SEVERITY_ORDER.map((sev) => {
                const group = report.weaknesses
                  .filter((w) => w.severity === sev)
                  .sort(compareWeaknessesBySeverity);
                if (group.length === 0) return null;
                return (
                  <div key={sev} className="border-b border-ink-100 last:border-b-0">
                    <div className="px-4 pt-3 pb-1 text-[10px] uppercase tracking-wide font-bold text-ink-500">
                      {sev} ({group.length})
                    </div>
                    <ul className="divide-y divide-ink-50">
                      {group.map((w) => (
                        <li key={w.id} className="px-4 py-2.5 text-sm">
                          <span className={clsx(
                            "text-[10px] uppercase tracking-wide font-bold mr-2 rounded px-1.5 py-0.5",
                            SEVERITY_STYLES[w.severity].badge,
                          )}>
                            {w.severity}
                          </span>
                          {w.description}
                          {w.unmatched && (
                            <span className="ml-2 rounded bg-ink-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-500">
                              not scored
                            </span>
                          )}
                          {w.quote && <div className="mt-1 text-xs text-ink-500 italic">&ldquo;{w.quote}&rdquo;</div>}
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
              {report.weaknesses.length === 0 && (
                <div className="px-4 py-3 text-sm text-ink-500">None.</div>
              )}
            </div>
          </Section>

          <Section title={`Appendix — documents reviewed (${report.documents.length})`}>
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

          <Section title="Appendix — service description">
            <div className="rounded-lg border border-ink-200 bg-white p-5 text-sm whitespace-pre-wrap">
              {report.description?.text || "—"}
            </div>
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
