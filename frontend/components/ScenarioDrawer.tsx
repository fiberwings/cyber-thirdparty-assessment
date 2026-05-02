"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ScenarioRead, ExpectedControlRead, Coverage, Effectiveness } from "@/lib/types";
import { api } from "@/lib/api";
import { ControlEditor } from "./ControlEditor";
import { useState } from "react";
import { bandColor, bandLabel } from "@/lib/utils";
import clsx from "clsx";

export function ScenarioDrawer({
  scenario,
  assessmentId,
  onClose,
}: {
  scenario: ScenarioRead;
  assessmentId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [inherentI, setInherentI] = useState(scenario.inherent_impact);
  const [inherentL, setInherentL] = useState(scenario.inherent_likelihood);

  const patch = useMutation({
    mutationFn: () =>
      api.patchScenario(scenario.id, { inherent_impact: inherentI, inherent_likelihood: inherentL }),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      qc.invalidateQueries({ queryKey: ["scenarios", assessmentId] });
      qc.invalidateQueries({ queryKey: ["report", assessmentId] });
    },
  });

  return (
    <aside className="fixed inset-y-0 right-0 w-[640px] bg-white border-l border-ink-200 shadow-2xl z-40 flex flex-col">
      <header className="p-5 border-b border-ink-200">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold">{scenario.code}</div>
            <h3 className="text-base font-semibold mt-0.5">{scenario.name}</h3>
          </div>
          <span className={clsx("rounded-md text-white text-[11px] font-semibold px-2 py-1", bandColor(scenario.score_band))}>
            {bandLabel(scenario.score_band)}
          </span>
          <button onClick={onClose} className="text-ink-500 hover:text-ink-900 text-xl leading-none">×</button>
        </div>
        <p className="mt-3 text-sm text-ink-700">{scenario.description}</p>

        <div className="mt-4 grid grid-cols-2 gap-3">
          <LevelSelect label="Inherent impact" value={inherentI} onChange={setInherentI} />
          <LevelSelect label="Inherent likelihood" value={inherentL} onChange={setInherentL} />
        </div>
        <button
          onClick={() => patch.mutate()}
          disabled={patch.isPending || (inherentI === scenario.inherent_impact && inherentL === scenario.inherent_likelihood)}
          className="mt-3 rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40"
        >
          {patch.isPending ? "Saving…" : "Save & recalculate"}
        </button>
      </header>

      <div className="flex-1 overflow-y-auto p-5 space-y-3">
        <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-1">Expected controls</div>
        {scenario.expected_controls.map((ec) => (
          <ControlEditor
            key={ec.id}
            control={ec}
            assessmentId={assessmentId}
            scenarioId={scenario.id}
          />
        ))}
        {scenario.rationale && (
          <div className="mt-6 rounded border border-ink-200 bg-ink-50 p-3">
            <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-1">Score explanation</div>
            <div className="text-sm text-ink-800 whitespace-pre-wrap">{scenario.rationale}</div>
          </div>
        )}
      </div>
    </aside>
  );
}

function LevelSelect({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <label className="block">
      <span className="text-[11px] text-ink-500 uppercase tracking-wide">{label}</span>
      <select
        className="mt-1 w-full rounded border border-ink-200 px-2 py-1 text-sm"
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value))}
      >
        <option value={1}>Low</option>
        <option value={2}>Moderate</option>
        <option value={3}>High</option>
        <option value={4}>Very High</option>
      </select>
    </label>
  );
}
