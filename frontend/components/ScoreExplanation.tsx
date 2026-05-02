"use client";

import { ReportOut } from "@/lib/types";
import { bandColor, bandLabel, compareScenarioScoresByRisk, formatPercent } from "@/lib/utils";
import clsx from "clsx";

export function ScoreExplanation({ report }: { report: ReportOut }) {
  const scenarios = [...report.scenarios].sort(compareScenarioScoresByRisk);
  return (
    <div className="space-y-3">
      {scenarios.map((s) => {
        const meta = report.meta_issues.filter((m) => m.scenario_code === s.code);
        return (
          <div key={s.code} className="rounded-lg border border-ink-200 bg-white p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1">
                <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold">{s.code}</div>
                <div className="text-sm font-semibold text-ink-900">{s.name}</div>
              </div>
              <span className={clsx("rounded-md text-white text-[11px] font-semibold px-2 py-1", bandColor(s.band))}>
                {bandLabel(s.band)}
              </span>
            </div>

            <div className="mt-3 grid grid-cols-4 gap-2 text-[11px]">
              <Cell label="Inherent I" value={String(s.inherent_impact)} />
              <Cell label="Inherent L" value={String(s.inherent_likelihood)} />
              <Cell label="Residual I" value={String(s.residual_impact)} />
              <Cell label="Residual L" value={String(s.residual_likelihood)} />
              <Cell label="Coverage idx." value={formatPercent(s.coverage_index)} />
              <Cell label="Likelihood reduction" value={`-${s.likelihood_reduction}`} />
              <Cell label="Meta uplift" value={`+${s.meta_uplift}`} />
              <Cell label="" value="" />
            </div>

            {meta.length > 0 && (
              <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
                <div className="font-medium mb-0.5">Transferral / assessment risk uplift</div>
                <ul className="space-y-0.5">
                  {meta.map((m) => (
                    <li key={m.id}>
                      <span className="font-medium">{m.kind.replace("_", " ")}</span>
                      {m.target_ref ? ` (${m.target_ref})` : ""} — +{m.weight}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {s.rationale && (
              <p className="mt-3 text-xs text-ink-700 whitespace-pre-wrap">{s.rationale}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-ink-50 px-2 py-1.5">
      <div className="text-ink-500 uppercase tracking-wide">{label}</div>
      <div className="text-ink-900 font-medium">{value}</div>
    </div>
  );
}
