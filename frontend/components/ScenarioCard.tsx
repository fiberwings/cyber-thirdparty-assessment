"use client";

import { ScenarioRead } from "@/lib/types";
import { LEVEL_ABBR, bandColor, bandLabel } from "@/lib/utils";
import clsx from "clsx";

export function ScenarioCard({ scenario, onOpen }: { scenario: ScenarioRead; onOpen: () => void }) {
  const evidenced = scenario.expected_controls.filter(
    (c) => c.assessment && c.assessment.coverage !== "none"
  ).length;
  const missing = scenario.expected_controls.filter(
    (c) => !c.assessment || c.assessment.coverage === "none"
  ).length;

  return (
    <button
      type="button"
      onClick={onOpen}
      className="text-left rounded-lg border border-ink-200 bg-white p-4 shadow-card hover:shadow-md hover:border-ink-300 transition w-full"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold">{scenario.code}</div>
          <div className="text-sm font-semibold text-ink-900 mt-0.5 line-clamp-2">{scenario.name}</div>
        </div>
        <span className={clsx("rounded-md text-white text-[11px] font-semibold px-2 py-1", bandColor(scenario.score_band))}>
          {bandLabel(scenario.score_band)}
        </span>
      </div>

      <p className="mt-3 text-xs text-ink-600 line-clamp-3">{scenario.description}</p>

      <div className="mt-4 grid grid-cols-3 gap-2 text-[10px]">
        <div className="rounded bg-ink-50 px-2 py-1.5">
          <div className="text-ink-500">Inherent I/L</div>
          <div className="text-ink-900 font-medium">
            {LEVEL_ABBR[scenario.inherent_impact]}/{LEVEL_ABBR[scenario.inherent_likelihood]}
          </div>
        </div>
        <div className="rounded bg-ink-50 px-2 py-1.5">
          <div className="text-ink-500">Residual I/L</div>
          <div className="text-ink-900 font-medium">
            {LEVEL_ABBR[scenario.residual_impact]}/{LEVEL_ABBR[scenario.residual_likelihood]}
          </div>
        </div>
        <div className="rounded bg-ink-50 px-2 py-1.5">
          <div className="text-ink-500">Controls</div>
          <div className="text-ink-900 font-medium">
            <span className="text-emerald-600">{evidenced}</span>
            <span className="text-ink-300 mx-1">/</span>
            <span className="text-rose-600">{missing}</span>
          </div>
        </div>
      </div>
      {scenario.source === "emergent" && (
        <div className="mt-3 inline-block rounded bg-purple-50 border border-purple-200 px-2 py-0.5 text-[10px] text-purple-700">
          emergent
        </div>
      )}
    </button>
  );
}
