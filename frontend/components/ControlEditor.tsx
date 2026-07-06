"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExpectedControlRead, Coverage, Effectiveness } from "@/lib/types";
import { api } from "@/lib/api";
import { CitationChip } from "./CitationChip";
import { useState, useEffect } from "react";

const COVERAGE_OPTIONS: Coverage[] = ["none", "partial", "full"];
const EFFECTIVENESS_OPTIONS: Effectiveness[] = ["unknown", "weak", "adequate", "strong"];

export function ControlEditor({
  control,
  assessmentId,
}: {
  control: ExpectedControlRead;
  assessmentId: number;
  scenarioId: number;
}) {
  const qc = useQueryClient();
  const ca = control.assessment;

  const [coverage, setCoverage] = useState<Coverage>(ca?.coverage || "none");
  const [effectiveness, setEffectiveness] = useState<Effectiveness>(ca?.effectiveness || "unknown");
  const [rationale, setRationale] = useState(ca?.rationale || "");

  useEffect(() => {
    setCoverage(ca?.coverage || "none");
    setEffectiveness(ca?.effectiveness || "unknown");
    setRationale(ca?.rationale || "");
  }, [ca?.coverage, ca?.effectiveness, ca?.rationale]);

  const dirty =
    coverage !== (ca?.coverage || "none") ||
    effectiveness !== (ca?.effectiveness || "unknown") ||
    rationale !== (ca?.rationale || "");

  const save = useMutation({
    mutationFn: () =>
      ca
        ? api.patchControlAssessment(ca.id, { coverage, effectiveness, rationale, is_locked_by_user: true })
        : api.upsertControlAssessment(control.id, { coverage, effectiveness, rationale }),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      qc.invalidateQueries({ queryKey: ["scenarios", assessmentId] });
      qc.invalidateQueries({ queryKey: ["report", assessmentId] });
    },
  });

  const remove = useMutation({
    mutationFn: () => api.deleteExpectedControl(control.id),
    onSuccess: async () => {
      await api.recalculate(assessmentId);
      qc.invalidateQueries({ queryKey: ["scenarios", assessmentId] });
      qc.invalidateQueries({ queryKey: ["report", assessmentId] });
    },
  });

  const onDelete = () => {
    const ok = window.confirm(
      `Remove control "${control.code}" from this scenario?\n\nCoverage, effectiveness, and citations are deleted with it. Weaknesses mapped to this control code stop affecting this scenario's score.`,
    );
    if (ok) remove.mutate();
  };

  const indicator =
    coverage === "full" ? "bg-emerald-500" : coverage === "partial" ? "bg-amber-500" : "bg-rose-500";

  return (
    <div className="rounded-md border border-ink-200 bg-white p-3">
      <div className="flex items-start gap-3">
        <span className={`mt-1 h-2 w-2 rounded-full ${indicator}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[11px] uppercase tracking-wide font-semibold text-ink-500">{control.code}</span>
            {ca?.is_locked_by_user && (
              <span className="text-[9px] text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-0.5">
                user-edited
              </span>
            )}
            <button
              onClick={onDelete}
              disabled={remove.isPending}
              title="Remove control from scenario"
              className="ml-auto text-ink-400 hover:text-rose-600 disabled:opacity-40 text-xs leading-none"
            >
              {remove.isPending ? "…" : "🗑"}
            </button>
          </div>
          <div className="text-sm font-medium text-ink-900">{control.name}</div>
          {control.description && (
            <div className="text-[11px] text-ink-500 mt-0.5">{control.description}</div>
          )}

          <div className="mt-3 grid grid-cols-2 gap-2">
            <SelectField
              label="Coverage"
              value={coverage}
              onChange={(v) => setCoverage(v as Coverage)}
              options={COVERAGE_OPTIONS}
            />
            <SelectField
              label="Effectiveness"
              value={effectiveness}
              onChange={(v) => setEffectiveness(v as Effectiveness)}
              options={EFFECTIVENESS_OPTIONS}
            />
          </div>

          {ca && ca.citations.length > 0 && (
            <div className="mt-3">
              <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">Citations</div>
              <div className="flex flex-wrap gap-1.5">
                {ca.citations.map((c, i) => (
                  <CitationChip key={i} citation={c} />
                ))}
              </div>
            </div>
          )}

          {ca && (ca.unresolved_citations?.length ?? 0) > 0 && (
            <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-2">
              <div className="text-[10px] uppercase tracking-wide text-amber-800 font-semibold mb-1">
                Citations that could not be located in the document
              </div>
              <ul className="space-y-1 text-[11px] text-amber-900">
                {ca.unresolved_citations.map((c, i) => (
                  <li key={i} className="italic">
                    &ldquo;{c.quote}&rdquo;
                    {c.page != null && <span className="not-italic text-amber-700"> — claimed p.{c.page}</span>}
                  </li>
                ))}
              </ul>
              <div className="mt-1 text-[10px] text-amber-700">
                The model quoted text that was not found verbatim — treat this evidence with caution.
              </div>
            </div>
          )}

          <textarea
            className="mt-3 w-full text-xs rounded border border-ink-200 px-2 py-1.5 min-h-[44px]"
            placeholder="Rationale (editable)…"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
          />

          <div className="mt-2 flex justify-end">
            <button
              onClick={() => save.mutate()}
              disabled={!dirty || save.isPending}
              className="rounded bg-ink-900 text-white text-[11px] font-medium px-3 py-1 hover:bg-ink-700 disabled:opacity-40"
            >
              {save.isPending ? "Saving…" : dirty ? "Save & recalc" : "Saved"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SelectField({
  label, value, onChange, options,
}: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-wide text-ink-500">{label}</span>
      <select
        className="mt-1 w-full rounded border border-ink-200 px-2 py-1 text-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}
