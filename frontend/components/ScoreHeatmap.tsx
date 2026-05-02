"use client";

import { ScenarioScoreRead, Band } from "@/lib/types";
import { bandColor } from "@/lib/utils";
import clsx from "clsx";

const LEVELS = [
  { v: 4, label: "Very High" },
  { v: 3, label: "High" },
  { v: 2, label: "Moderate" },
  { v: 1, label: "Low" },
];

const MATRIX: Band[][] = [
  // L=1         L=2          L=3         L=4
  ["Low",      "Low",        "Moderate", "Moderate"],   // I=1
  ["Low",      "Moderate",   "Moderate", "High"],       // I=2
  ["Moderate", "Moderate",   "High",     "VeryHigh"],   // I=3
  ["Moderate", "High",       "VeryHigh", "VeryHigh"],   // I=4
];

export function ScoreHeatmap({ scenarios }: { scenarios: ScenarioScoreRead[] }) {
  // count scenarios per cell for stacking dots
  const cells: Record<string, ScenarioScoreRead[]> = {};
  for (const s of scenarios) {
    const key = `${s.residual_impact}-${s.residual_likelihood}`;
    cells[key] ||= [];
    cells[key].push(s);
  }

  return (
    <div className="rounded-lg border border-ink-200 bg-white p-5">
      <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Risk heatmap (residual)</div>
      <div className="flex">
        <div className="flex flex-col justify-between mr-2 py-1 text-[10px] text-ink-500 uppercase tracking-wide">
          <span className="-rotate-90 origin-top-left translate-y-12">Impact</span>
        </div>
        <div className="flex-1">
          <div className="grid grid-cols-[auto_repeat(4,1fr)] gap-1">
            {LEVELS.map((row) => (
              <>
                <div key={`lab-${row.v}`} className="text-right text-[10px] text-ink-500 pr-2 self-center">
                  {row.label}
                </div>
                {[1, 2, 3, 4].map((col) => {
                  const band = MATRIX[row.v - 1][col - 1];
                  const stacked = cells[`${row.v}-${col}`] || [];
                  return (
                    <div
                      key={`c-${row.v}-${col}`}
                      className={clsx(
                        "relative aspect-square rounded text-white font-semibold flex items-center justify-center text-xs",
                        bandColor(band),
                      )}
                    >
                      {stacked.length > 0 && (
                        <div className="text-base font-bold">{stacked.length}</div>
                      )}
                    </div>
                  );
                })}
              </>
            ))}
            <div />
            {LEVELS.map((c) => (
              <div key={`bot-${c.v}`} className="text-center text-[10px] text-ink-500 mt-1 uppercase tracking-wide">
                {c.label}
              </div>
            ))}
          </div>
          <div className="text-center text-[10px] text-ink-500 mt-2 uppercase tracking-wide">Likelihood →</div>
        </div>
      </div>
    </div>
  );
}
