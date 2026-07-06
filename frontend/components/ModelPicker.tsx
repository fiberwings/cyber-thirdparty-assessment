"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useState } from "react";

const STAGES: { key: string; label: string; profile: "fast" | "reasoner" }[] = [
  { key: "scoping",      label: "Scoping (Q&A)",       profile: "fast" },
  { key: "scenarios",    label: "Scenario gen",        profile: "reasoner" },
  { key: "gap_analysis", label: "Gap analysis",        profile: "reasoner" },
  { key: "weaknesses",   label: "Weakness synth.",     profile: "reasoner" },
  { key: "narrative",    label: "Narratives",          profile: "fast" },
  { key: "executive_summary", label: "Exec summary",   profile: "reasoner" },
];

export function ModelPicker({ assessmentId, overrides }: { assessmentId: number; overrides: Record<string, string> }) {
  const qc = useQueryClient();
  const { data: profiles } = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });
  const [open, setOpen] = useState(false);
  const mutation = useMutation({
    mutationFn: (next: Record<string, string>) => api.setModelOverrides(assessmentId, next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["assessment", assessmentId] }),
  });

  if (!profiles) return null;
  const fast = profiles.find((p) => p.name === "fast")!;
  const reasoner = profiles.find((p) => p.name === "reasoner")!;
  const optionsFor = (profile: "fast" | "reasoner") =>
    profile === "fast"
      ? [fast.default_model, ...fast.alternatives.filter((m) => m !== fast.default_model)]
      : [reasoner.default_model, ...reasoner.alternatives.filter((m) => m !== reasoner.default_model)];

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-md border border-ink-200 bg-white px-3 py-1.5 text-xs font-medium text-ink-700 hover:border-ink-300"
      >
        <span className="h-2 w-2 rounded-full bg-emerald-500" />
        Models
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-[360px] rounded-lg border border-ink-200 bg-white shadow-xl z-30">
          <div className="px-4 py-3 border-b border-ink-100">
            <div className="text-sm font-semibold">Model routing (per stage)</div>
            <div className="text-xs text-ink-500 mt-0.5">Cheap/fast for ingest & Q&A. Heavy reasoner for analysis.</div>
          </div>
          <div className="px-4 py-3 space-y-3">
            {STAGES.map((stage) => {
              const opts = optionsFor(stage.profile);
              const value = overrides[stage.key] || (stage.profile === "fast" ? fast.default_model : reasoner.default_model);
              return (
                <div key={stage.key} className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-medium text-ink-700">{stage.label}</div>
                    <div className="text-[10px] uppercase tracking-wide text-ink-500">{stage.profile}</div>
                  </div>
                  <select
                    className="text-xs border border-ink-200 rounded px-2 py-1 bg-white max-w-[200px] truncate"
                    value={value}
                    onChange={(e) => mutation.mutate({ ...overrides, [stage.key]: e.target.value })}
                  >
                    {opts.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </div>
              );
            })}
          </div>
          <div className="px-4 py-3 border-t border-ink-100 flex justify-end">
            <button
              className="text-xs text-ink-600 hover:text-ink-900"
              onClick={() => setOpen(false)}
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
