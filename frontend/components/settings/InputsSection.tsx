"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useWorkflow } from "@/lib/useWorkflow";
import { hasStandards, normalizeProfile } from "@/lib/settings";
import { EMPTY_STANDARDS, StandardsProfile } from "@/lib/types";
import { Chip, INPUT_CLASS, SettingsRow, SettingsSection } from "./SettingsSection";

const RUNNING_MSG = "A job is running — wait for it to finish.";

// Assessment-level inputs (R7): the analysis date every freshness judgement
// is made against, and the assessor's own standards. Both feed every prompt,
// so saving stamps the steps that consumed the old values stale (backend
// patch_settings); nothing is re-run here. Save is explicit for that reason.
export function InputsSection({ assessmentId }: { assessmentId: number }) {
  const qc = useQueryClient();
  const { assessment: a, anyRunning } = useWorkflow(assessmentId);

  const [asOf, setAsOf] = useState("");
  const [pinned, setPinned] = useState(false);
  const [profile, setProfile] = useState<StandardsProfile>(EMPTY_STANDARDS);

  useEffect(() => {
    if (!a) return;
    setAsOf(a.as_of_date);
    setPinned(a.as_of_date_set);
    setProfile({ ...EMPTY_STANDARDS, ...a.standards_profile });
  }, [a?.as_of_date, a?.as_of_date_set, a?.standards_profile, a]);

  const save = useMutation({
    mutationFn: () =>
      api.patchSettings(assessmentId, {
        ...(pinned ? { as_of_date: asOf } : { clear_as_of_date: true }),
        standards_profile: profile,
      }),
    onSuccess: (next) => qc.setQueryData(["assessment", assessmentId], next),
  });

  // Dirty = local form differs from what the server holds (canonical JSON).
  const canonical = (p: boolean, d: string, s: StandardsProfile | undefined) =>
    JSON.stringify({ pinned: p, asOf: p ? d : null, profile: normalizeProfile(s) });
  const dirty = !!a && canonical(pinned, asOf, profile) !== canonical(a.as_of_date_set, a.as_of_date, a.standards_profile);
  const locked = anyRunning;

  const list = (v: string[]) => v.join("\n");
  const parseList = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
  const num = (v: number | null | undefined) => (v == null ? "" : String(v));
  const parseNum = (s: string) => (s.trim() === "" ? null : Math.max(0, parseInt(s) || 0));

  const textarea = (value: string[], onChange: (v: string[]) => void, placeholder?: string) => (
    <textarea
      className={`${INPUT_CLASS} w-full min-h-[60px]`}
      value={list(value)}
      onChange={(e) => onChange(parseList(e.target.value))}
      placeholder={placeholder}
    />
  );
  const numInput = (value: number | null | undefined, onChange: (v: number | null) => void) => (
    <input
      type="number"
      min={0}
      className={`${INPUT_CLASS} w-28`}
      value={num(value)}
      onChange={(e) => onChange(parseNum(e.target.value))}
    />
  );

  return (
    <SettingsSection
      title="Assessment inputs"
      description="The analysis date and the assessor's own standards. Both feed every prompt."
      status={
        a ? (
          hasStandards(a.standards_profile) ? (
            <Chip tone="emerald">standards set</Chip>
          ) : (
            <Chip tone="muted">no standards</Chip>
          )
        ) : null
      }
      footer={
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <div className="text-xs min-w-0">
              {locked ? (
                <span className="text-ink-500">{RUNNING_MSG}</span>
              ) : save.isError ? (
                <span className="text-risk-high">{describeError(save.error)}</span>
              ) : save.isSuccess && !dirty ? (
                <span className="text-emerald-700">Saved</span>
              ) : dirty ? (
                <span className="text-ink-500">Unsaved changes</span>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => save.mutate()}
              disabled={locked || !dirty || save.isPending || (pinned && !asOf)}
              className="shrink-0 rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40"
            >
              {save.isPending ? "Saving…" : "Save inputs"}
            </button>
          </div>
          <p className="text-[11px] text-ink-500">
            Changing these does not re-run anything: stages run afterwards use the new values and completed steps that
            consumed the old ones are flagged stale.
          </p>
        </div>
      }
    >
      {!a ? (
        <div className="py-3 text-xs text-ink-500">Loading…</div>
      ) : (
        <fieldset disabled={locked} className="divide-y divide-ink-100 min-w-0">
          <SettingsRow
            label="Analysis date"
            hint="freshness reference"
            status={pinned ? <Chip tone="amber">pinned</Chip> : <Chip tone="muted">today</Chip>}
          >
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <label className="flex items-center gap-1.5 text-xs text-ink-700 whitespace-nowrap">
                <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
                Pin to a date
              </label>
              <input
                type="date"
                value={asOf}
                disabled={!pinned}
                onChange={(e) => setAsOf(e.target.value)}
                className={INPUT_CLASS}
              />
            </div>
            <p className="text-[11px] text-ink-500 mt-1">
              Every prompt&apos;s &quot;Analysis date&quot; and every staleness judgement use this date (not the run
              date). Leave unpinned to use today.
            </p>
          </SettingsRow>

          <SettingsRow label="Required attestations" hint="one per line">
            {textarea(profile.required_attestations, (v) => setProfile({ ...profile, required_attestations: v }), "SOC 2 Type 2\nISO/IEC 27001")}
          </SettingsRow>
          <SettingsRow label="Allowed data residency" hint="one per line">
            {textarea(profile.allowed_residency, (v) => setProfile({ ...profile, allowed_residency: v }), "EEA\nUnited Kingdom")}
          </SettingsRow>
          <SettingsRow label="Attestation max age" hint="months">
            {numInput(profile.attestation_max_age_months, (v) => setProfile({ ...profile, attestation_max_age_months: v }))}
          </SettingsRow>
          <SettingsRow label="Pen-test max age" hint="months">
            {numInput(profile.pentest_max_age_months, (v) => setProfile({ ...profile, pentest_max_age_months: v }))}
          </SettingsRow>
          <SettingsRow label="Policy review cadence" hint="months">
            {numInput(profile.policy_review_months, (v) => setProfile({ ...profile, policy_review_months: v }))}
          </SettingsRow>
          <SettingsRow label="Record retention target" hint="years">
            {numInput(profile.retention_years, (v) => setProfile({ ...profile, retention_years: v }))}
          </SettingsRow>
          <SettingsRow label="MFA policy">
            <input
              className={`${INPUT_CLASS} w-full`}
              value={profile.mfa_policy || ""}
              onChange={(e) => setProfile({ ...profile, mfa_policy: e.target.value || null })}
              placeholder="MFA on all accounts; phishing-resistant for privileged"
            />
          </SettingsRow>
          <SettingsRow label="Vulnerability SLA" hint="days: critical / high / medium">
            <div className="flex gap-2">
              {(["critical_days", "high_days", "medium_days"] as const).map((k) => (
                <input
                  key={k}
                  type="number"
                  min={0}
                  aria-label={k.replace("_days", "")}
                  className={`${INPUT_CLASS} w-20`}
                  value={num(profile.vuln_remediation_sla?.[k])}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      vuln_remediation_sla: { ...(profile.vuln_remediation_sla || {}), [k]: parseNum(e.target.value) },
                    })
                  }
                />
              ))}
            </div>
          </SettingsRow>
          <SettingsRow label="Other requirements" hint="one per line">
            {textarea(profile.other_requirements, (v) => setProfile({ ...profile, other_requirements: v }))}
          </SettingsRow>
        </fieldset>
      )}
    </SettingsSection>
  );
}
