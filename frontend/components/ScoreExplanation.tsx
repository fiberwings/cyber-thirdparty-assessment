"use client";

import { ReportOut, ScenarioScoreRead } from "@/lib/types";
import { bandColor, bandLabel, compareScenarioScoresByRisk, formatPercent } from "@/lib/utils";
import clsx from "clsx";

// One human-readable line naming what actually drove the residual band.
function driverLine(s: ScenarioScoreRead): string {
  const parts = [
    `Coverage ${formatPercent(s.coverage_index)} → −${s.likelihood_reduction} likelihood`,
  ];
  if (s.combined_uplift > 0) {
    const causes: string[] = [];
    if (s.weakness_uplift_raw > 0) causes.push("weaknesses");
    if (s.meta_uplift_raw > 0) causes.push("assessment gaps");
    parts.push(`uplift +${s.combined_uplift} from ${causes.join(" & ") || "issues"}`);
  }
  if (s.effectiveness_downgrades.length > 0) {
    parts.push(`${s.effectiveness_downgrades.length} control(s) downgraded by high/critical findings`);
  }
  return parts.join(" · ");
}

export function ScoreExplanation({
  report,
  highlightCode,
}: {
  report: ReportOut;
  highlightCode?: string | null;
}) {
  const scenarios = [...report.scenarios].sort(compareScenarioScoresByRisk);
  return (
    <div className="space-y-2">
      {scenarios.map((s) => {
        const meta = report.meta_issues.filter((m) => m.scenario_code === s.code);
        const open = highlightCode === s.code;
        return (
          <details
            key={`${s.code}-${open}`}
            id={`scenario-${s.code}`}
            open={open}
            className={clsx(
              "group rounded-lg border bg-white scroll-mt-4",
              open ? "border-ink-400" : "border-ink-200",
            )}
          >
            <summary className="flex items-center gap-3 px-4 py-3 cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden">
              <Caret />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold font-mono">
                    {s.code}
                  </span>
                  <span className="text-sm font-semibold text-ink-900 truncate">{s.name}</span>
                </div>
                <div className="text-[11px] text-ink-500 mt-0.5">{driverLine(s)}</div>
              </div>
              <span className={clsx("rounded-md text-white text-[11px] font-semibold px-2 py-1 shrink-0", bandColor(s.band))}>
                {bandLabel(s.band)}
              </span>
            </summary>

            <div className="border-t border-ink-100 px-4 py-3">
              <div className="grid grid-cols-4 gap-2 text-[11px]">
                <Cell label="Inherent I" value={String(s.inherent_impact)} />
                <Cell label="Inherent L" value={String(s.inherent_likelihood)} />
                <Cell label="Residual I" value={String(s.residual_impact)} />
                <Cell label="Residual L" value={String(s.residual_likelihood)} />
                <Cell label="Coverage idx." value={formatPercent(s.coverage_index)} />
                <Cell label="Likelihood reduction" value={`-${s.likelihood_reduction}`} />
                <Cell
                  label="Uplift applied"
                  value={`+${s.combined_uplift}`}
                  hint={`meta ${s.meta_uplift_raw.toFixed(2)} + weaknesses ${s.weakness_uplift_raw.toFixed(2)}, capped`}
                />
                <Cell
                  label="Downgrades"
                  value={s.effectiveness_downgrades.length > 0 ? s.effectiveness_downgrades.join(", ") : "—"}
                />
              </div>

              {meta.length > 0 && (
                <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
                  <div className="font-medium mb-0.5">Assessment-quality uplift drivers</div>
                  <ul className="space-y-0.5">
                    {meta.map((m) => (
                      <li key={m.id}>
                        <span className="font-medium">{m.kind.replace(/_/g, " ")}</span>
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
          </details>
        );
      })}
    </div>
  );
}

function Cell({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded bg-ink-50 px-2 py-1.5" title={hint}>
      <div className="text-ink-500 uppercase tracking-wide">{label}</div>
      <div className="text-ink-900 font-medium truncate">{value}</div>
      {hint && <div className="text-[10px] text-ink-400 truncate">{hint}</div>}
    </div>
  );
}

function Caret() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      className="shrink-0 text-ink-400 transition-transform group-open:rotate-90"
      aria-hidden
    >
      <path d="M3 1l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </svg>
  );
}
