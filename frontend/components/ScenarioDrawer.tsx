"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ScenarioRead } from "@/lib/types";
import { api, describeError } from "@/lib/api";
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

  // Edits stamp downstream steps stale (and are refused while a job runs),
  // so the assessment's phases are refreshed alongside the scores.
  const invalidateScores = () => {
    qc.invalidateQueries({ queryKey: ["scenarios", assessmentId] });
    qc.invalidateQueries({ queryKey: ["report", assessmentId] });
    qc.invalidateQueries({ queryKey: ["assessment", assessmentId] });
  };

  const patch = useMutation({
    mutationFn: () =>
      api.patchScenario(scenario.id, { inherent_impact: inherentI, inherent_likelihood: inherentL }),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      invalidateScores();
    },
  });

  const remove = useMutation({
    mutationFn: () => api.deleteScenario(scenario.id),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      invalidateScores();
      onClose();
    },
  });
  const editError = patch.isError ? describeError(patch.error) : remove.isError ? describeError(remove.error) : null;

  const onDeleteScenario = () => {
    const ok = window.confirm(
      `Delete scenario "${scenario.code}"?\n\nThis removes the scenario, its expected controls, and all associated evidence. This cannot be undone.`,
    );
    if (ok) remove.mutate();
  };

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
          <button
            onClick={onDeleteScenario}
            disabled={remove.isPending}
            title="Delete scenario"
            className="text-ink-400 hover:text-rose-600 disabled:opacity-40 text-sm leading-none"
          >
            {remove.isPending ? "…" : "🗑"}
          </button>
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
        {editError && <div className="mt-2 text-xs text-risk-high">{editError}</div>}
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

        <AddControlForm scenarioId={scenario.id} assessmentId={assessmentId} />

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

function AddControlForm({
  scenarioId,
  assessmentId,
}: {
  scenarioId: number;
  assessmentId: number;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setCode("");
    setName("");
    setDescription("");
    setError(null);
  };

  const create = useMutation({
    mutationFn: () =>
      api.createExpectedControl(scenarioId, {
        code: code.trim().toUpperCase(),
        name: name.trim(),
        description: description.trim() || undefined,
      }),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      qc.invalidateQueries({ queryKey: ["scenarios", assessmentId] });
      qc.invalidateQueries({ queryKey: ["report", assessmentId] });
      qc.invalidateQueries({ queryKey: ["assessment", assessmentId] });
      reset();
      setOpen(false);
    },
    onError: (e: unknown) => setError(describeError(e)),
  });

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="w-full rounded-md border border-dashed border-ink-300 text-ink-600 hover:text-ink-900 hover:border-ink-500 text-xs font-medium py-2 transition"
      >
        + Add expected control
      </button>
    );
  }

  const canSubmit = code.trim().length > 0 && name.trim().length > 0 && !create.isPending;

  return (
    <div className="rounded-md border border-ink-300 bg-ink-50 p-3 space-y-2">
      <div className="text-[11px] uppercase tracking-wide text-ink-500 font-semibold">
        Add expected control
      </div>
      <input
        autoFocus
        className="w-full rounded border border-ink-200 px-2 py-1 text-xs font-mono uppercase"
        placeholder="CODE (e.g. MFA_REQUIRED)"
        value={code}
        onChange={(e) => setCode(e.target.value)}
        onBlur={(e) => setCode(e.target.value.trim().toUpperCase())}
      />
      <input
        className="w-full rounded border border-ink-200 px-2 py-1 text-sm"
        placeholder="Control name"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <textarea
        className="w-full rounded border border-ink-200 px-2 py-1 text-xs min-h-[44px]"
        placeholder="Description (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <p className="text-[10px] text-ink-500 leading-snug">
        Newly added controls have no coverage assessment yet, so they reduce coverage_index until you assess them.
      </p>
      {error && (
        <div className="text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1">
          {error}
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button
          onClick={() => {
            reset();
            setOpen(false);
          }}
          disabled={create.isPending}
          className="rounded text-ink-600 hover:text-ink-900 text-xs px-2 py-1 disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          onClick={() => {
            setError(null);
            create.mutate();
          }}
          disabled={!canSubmit}
          className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1 hover:bg-ink-700 disabled:opacity-40"
        >
          {create.isPending ? "Adding…" : "Add control"}
        </button>
      </div>
    </div>
  );
}
