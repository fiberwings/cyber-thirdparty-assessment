"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useWorkflow } from "@/lib/useWorkflow";
import { STAGES, customModelCount, effectiveModel, isCustomModel, modelOptions, profileFor } from "@/lib/settings";
import { Chip, INPUT_CLASS, SettingsRow, SettingsSection } from "./SettingsSection";

// Which provider a model ref routes to, from its scheme prefix (mirrors
// backend app/ai/providers/registry.py: bare ids are OpenRouter).
function providerOf(ref: string): "Azure" | "Foundry" | "OpenRouter" {
  if (ref.startsWith("azure:")) return "Azure";
  if (ref.startsWith("foundry:")) return "Foundry";
  return "OpenRouter";
}

// Per-stage model routing. Each change is saved immediately (the endpoint
// validates the model's capability for the stage's profile and answers
// 422 with a reason). Model routing is deliberately not gated on running
// jobs: the backend allows it and a change only affects later runs.
export function ModelsSection({ assessmentId }: { assessmentId: number }) {
  const qc = useQueryClient();
  const { assessment } = useWorkflow(assessmentId);
  const { data: profiles } = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });
  const [savedKey, setSavedKey] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: ({ next }: { key: string; next: Record<string, string> }) => api.setModelOverrides(assessmentId, next),
    onSuccess: (a, { key }) => {
      qc.setQueryData(["assessment", assessmentId], a);
      setSavedKey(key);
    },
  });

  useEffect(() => {
    if (!savedKey) return;
    const t = setTimeout(() => setSavedKey(null), 2000);
    return () => clearTimeout(t);
  }, [savedKey]);

  const overrides = assessment?.model_overrides || {};
  const custom = customModelCount(overrides, profiles);

  const resetAll = () => {
    if (!profiles) return;
    const next: Record<string, string> = {};
    for (const s of STAGES) {
      const def = profileFor(profiles, s.profile)?.default_model;
      if (def) next[s.key] = def;
    }
    mutation.mutate({ key: "*", next });
  };

  return (
    <SettingsSection
      title="Model routing"
      description="Cheap/fast profile for ingest and Q&A, heavy reasoner for analysis. Per stage, for this assessment only."
      status={
        profiles ? (
          custom > 0 ? <Chip tone="amber">{custom} custom</Chip> : <Chip tone="muted">All defaults</Chip>
        ) : null
      }
      footer={
        <div className="flex items-center justify-between gap-3">
          <div className="text-xs min-w-0">
            {mutation.isError ? (
              <span className="text-risk-high">{describeError(mutation.error)}</span>
            ) : savedKey === "*" ? (
              <span className="text-emerald-700">Saved</span>
            ) : (
              <span className="text-ink-500">Applies to the next run of each stage. Completed steps are not re-run.</span>
            )}
          </div>
          {custom > 0 && (
            <button
              type="button"
              onClick={resetAll}
              disabled={mutation.isPending}
              className="shrink-0 rounded border border-ink-300 text-ink-700 text-xs font-medium px-3 py-1.5 hover:bg-ink-50 disabled:opacity-40"
            >
              Reset to defaults
            </button>
          )}
        </div>
      }
    >
      {!profiles || !assessment ? (
        <div className="py-3 text-xs text-ink-500">Loading…</div>
      ) : (
        STAGES.map((stage) => {
          const opts = modelOptions(profiles, stage.profile);
          const value = effectiveModel(stage.key, overrides, profiles) ?? "";
          const isCustom = isCustomModel(stage.key, overrides, profiles);
          return (
            <SettingsRow
              key={stage.key}
              label={stage.label}
              hint={stage.profile}
              status={
                <span className="inline-flex items-center gap-1">
                  {value && providerOf(value) !== "OpenRouter" && <Chip tone="muted">{providerOf(value)}</Chip>}
                  {savedKey === stage.key ? (
                    <span className="text-xs text-emerald-700">Saved</span>
                  ) : isCustom ? (
                    <Chip tone="amber">custom</Chip>
                  ) : (
                    <Chip tone="muted">default</Chip>
                  )}
                </span>
              }
            >
              <select
                className={`${INPUT_CLASS} w-full max-w-[320px] truncate`}
                value={value}
                title={value}
                disabled={mutation.isPending}
                aria-label={`Model for ${stage.label}`}
                onChange={(e) => mutation.mutate({ key: stage.key, next: { ...overrides, [stage.key]: e.target.value } })}
              >
                {opts.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </SettingsRow>
          );
        })
      )}
    </SettingsSection>
  );
}
