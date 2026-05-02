"use client";

import { Fragment } from "react";
import { ScenarioScoreRead, Band } from "@/lib/types";
import { bandColor, bandLabel } from "@/lib/utils";
import clsx from "clsx";

const IMPACT_ROWS = [
  { v: 4, label: "Very High" },
  { v: 3, label: "High" },
  { v: 2, label: "Moderate" },
  { v: 1, label: "Low" },
];

const LIKELIHOOD_COLS = [
  { v: 1, label: "Low" },
  { v: 2, label: "Moderate" },
  { v: 3, label: "High" },
  { v: 4, label: "Very High" },
];

const MATRIX: Band[][] = [
  // L=1         L=2          L=3         L=4
  ["Low",      "Low",        "Moderate", "Moderate"],   // I=1
  ["Low",      "Moderate",   "Moderate", "High"],       // I=2
  ["Moderate", "Moderate",   "High",     "VeryHigh"],   // I=3
  ["Moderate", "High",       "VeryHigh", "VeryHigh"],   // I=4
];

const BAND_LEGEND: Band[] = ["Low", "Moderate", "High", "VeryHigh"];

export function ScoreHeatmap({ scenarios }: { scenarios: ScenarioScoreRead[] }) {
  const cells: Record<string, ScenarioScoreRead[]> = {};
  for (const s of scenarios) {
    const key = `${s.residual_impact}-${s.residual_likelihood}`;
    cells[key] ||= [];
    cells[key].push(s);
  }

  return (
    <div className="rounded-xl border border-ink-200 bg-white p-5 shadow-card">
      <div className="flex items-center justify-between mb-4">
        <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
          Risk heatmap (residual)
        </div>
        <div className="text-[10px] text-ink-500">
          {scenarios.length} scenario{scenarios.length === 1 ? "" : "s"}
        </div>
      </div>

      <div className="flex gap-3">
        <div className="flex items-center">
          <span className="text-[10px] uppercase tracking-wider text-ink-500 font-semibold whitespace-nowrap [writing-mode:vertical-rl] rotate-180">
            Impact
          </span>
        </div>

        <div className="flex-1">
          <div className="grid grid-cols-[auto_repeat(4,minmax(0,1fr))] gap-1.5">
            {IMPACT_ROWS.map((row) => (
              <Fragment key={row.v}>
                <div className="text-right text-[10px] text-ink-500 pr-2 self-center font-medium">
                  {row.label}
                </div>
                {LIKELIHOOD_COLS.map((col) => {
                  const band = MATRIX[row.v - 1][col.v - 1];
                  const stacked = cells[`${row.v}-${col.v}`] || [];
                  const header = `${row.label} impact × ${col.label} likelihood — ${bandLabel(band)}`;
                  const tooltip = stacked.length
                    ? `${header}\n${stacked.map((s) => `• ${s.code}`).join("\n")}`
                    : header;
                  return (
                    <div
                      key={col.v}
                      className={clsx(
                        "relative aspect-square rounded-md flex items-center justify-center p-1 ring-1 ring-inset ring-black/5 transition overflow-hidden",
                        bandColor(band),
                        stacked.length === 0 && "opacity-40",
                      )}
                      title={tooltip}
                    >
                      {stacked.length === 1 && (
                        <span className="block max-w-full truncate rounded bg-white/95 text-ink-900 text-[10px] font-bold px-1.5 py-0.5 leading-none shadow-sm">
                          {stacked[0].code}
                        </span>
                      )}
                      {stacked.length > 1 && (
                        <span className="rounded-full bg-white/95 text-ink-900 text-xs font-bold h-6 w-6 flex items-center justify-center shadow-sm">
                          {stacked.length}
                        </span>
                      )}
                    </div>
                  );
                })}
              </Fragment>
            ))}

            <div />
            {LIKELIHOOD_COLS.map((c) => (
              <div
                key={`bot-${c.v}`}
                className="text-center text-[10px] text-ink-500 mt-1 font-medium"
              >
                {c.label}
              </div>
            ))}
          </div>

          <div className="text-center text-[10px] uppercase tracking-wider text-ink-500 font-semibold mt-2">
            Likelihood →
          </div>
        </div>
      </div>

      <div className="mt-4 pt-4 border-t border-ink-100 flex flex-wrap items-center gap-3">
        {BAND_LEGEND.map((b) => (
          <div key={b} className="flex items-center gap-1.5 text-[11px] text-ink-600">
            <span className={clsx("h-2.5 w-2.5 rounded-sm", bandColor(b))} />
            {bandLabel(b)}
          </div>
        ))}
      </div>
    </div>
  );
}
