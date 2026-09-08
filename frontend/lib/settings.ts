// Pure helpers for the per-assessment Settings page and its nav/summary
// surfaces. No React here so LeftNav, the scoping summary and the page
// itself all derive "is this assessment on defaults?" from one place.

import type { Assessment, ModelProfile, StandardsProfile } from "./types";

export type ModelProfileName = "fast" | "reasoner";

// The six routed stages. Keys must equal the backend's ModelOverrides
// fields (backend/app/schemas/api.py) and the fast/reasoner split must
// equal patch_model_overrides (backend/app/api/assessments.py); a backend
// test (tests/test_frontend_stages_sync.py) pins both.
export const STAGES: { key: string; label: string; profile: ModelProfileName }[] = [
  { key: "scoping",           label: "Scoping (Q&A)",      profile: "fast" },
  { key: "scenarios",         label: "Scenario generation", profile: "reasoner" },
  { key: "gap_analysis",      label: "Gap analysis",       profile: "reasoner" },
  { key: "weaknesses",        label: "Weakness synthesis", profile: "reasoner" },
  { key: "narrative",         label: "Narratives",         profile: "fast" },
  { key: "executive_summary", label: "Executive summary",  profile: "reasoner" },
];

export function profileFor(profiles: ModelProfile[], name: ModelProfileName): ModelProfile | undefined {
  return profiles.find((p) => p.name === name);
}

// Options for a stage's select: the profile default first, then the
// configured alternatives (deduplicated).
export function modelOptions(profiles: ModelProfile[], name: ModelProfileName): string[] {
  const p = profileFor(profiles, name);
  if (!p) return [];
  return [p.default_model, ...p.alternatives.filter((m) => m !== p.default_model)];
}

// The model a stage will run with: its override, else the profile default.
export function effectiveModel(
  stageKey: string,
  overrides: Record<string, string>,
  profiles: ModelProfile[],
): string | undefined {
  const stage = STAGES.find((s) => s.key === stageKey);
  if (!stage) return undefined;
  return overrides[stageKey] || profileFor(profiles, stage.profile)?.default_model;
}

export function isCustomModel(stageKey: string, overrides: Record<string, string>, profiles: ModelProfile[]): boolean {
  const stage = STAGES.find((s) => s.key === stageKey);
  const override = overrides[stageKey];
  if (!stage || !override) return false;
  const def = profileFor(profiles, stage.profile)?.default_model;
  // Until profiles load we cannot tell an override from a default; report
  // nothing custom rather than a false alarm.
  return def !== undefined && override !== def;
}

// Stages whose effective model differs from the profile default. An
// override equal to the default is not custom.
export function customModelCount(overrides: Record<string, string> | undefined, profiles: ModelProfile[] | undefined): number {
  if (!overrides || !profiles) return 0;
  return STAGES.filter((s) => isCustomModel(s.key, overrides, profiles)).length;
}

export function hasStandards(p: StandardsProfile | undefined): boolean {
  if (!p) return false;
  return (
    p.required_attestations.length > 0 ||
    p.allowed_residency.length > 0 ||
    p.other_requirements.length > 0 ||
    !!p.mfa_policy ||
    p.attestation_max_age_months != null ||
    p.pentest_max_age_months != null ||
    p.policy_review_months != null ||
    p.retention_years != null ||
    !!(p.vuln_remediation_sla && Object.values(p.vuln_remediation_sla).some((v) => v != null))
  );
}

export function analysisDateLine(a: Pick<Assessment, "as_of_date" | "as_of_date_set">): string {
  return `Analysis date ${a.as_of_date}${a.as_of_date_set ? " (pinned)" : " (today)"}`;
}

export function standardsLine(p: StandardsProfile | undefined): string {
  return hasStandards(p) ? "assessor standards set" : "no assessor standards";
}

export function modelsLine(overrides: Record<string, string> | undefined, profiles: ModelProfile[] | undefined): string | null {
  if (!profiles) return null;
  const n = customModelCount(overrides, profiles);
  if (n === 0) return "default models";
  return `${n} custom model${n === 1 ? "" : "s"}`;
}

// One-line digest used on the scoping page: "Analysis date … · assessor
// standards set · 2 custom models". The model part is omitted until the
// profiles query has loaded.
export function settingsSummary(a: Assessment, profiles: ModelProfile[] | undefined): string {
  const parts = [analysisDateLine(a), standardsLine(a.standards_profile)];
  const models = modelsLine(a.model_overrides, profiles);
  if (models) parts.push(models);
  return parts.join(" · ");
}

// Canonical shape for change detection: fixed key order, null for absent
// scalars, and a null SLA when no day is set. Equal inputs → equal JSON.
export function normalizeProfile(p: StandardsProfile | undefined): StandardsProfile {
  const sla = p?.vuln_remediation_sla;
  const slaNorm = {
    critical_days: sla?.critical_days ?? null,
    high_days: sla?.high_days ?? null,
    medium_days: sla?.medium_days ?? null,
  };
  const anySla = Object.values(slaNorm).some((v) => v != null);
  return {
    required_attestations: p?.required_attestations ?? [],
    attestation_max_age_months: p?.attestation_max_age_months ?? null,
    pentest_max_age_months: p?.pentest_max_age_months ?? null,
    policy_review_months: p?.policy_review_months ?? null,
    retention_years: p?.retention_years ?? null,
    allowed_residency: p?.allowed_residency ?? [],
    mfa_policy: p?.mfa_policy || null,
    vuln_remediation_sla: anySla ? slaNorm : null,
    other_requirements: p?.other_requirements ?? [],
  };
}
