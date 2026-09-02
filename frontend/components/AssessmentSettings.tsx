"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { EMPTY_STANDARDS, StandardsProfile } from "@/lib/types";

// Assessment-level inputs (R7): the analysis date every freshness judgement
// is made against, and the assessor's own standards. Both feed every prompt.
export function AssessmentSettings({ assessmentId }: { assessmentId: number }) {
  const qc = useQueryClient();
  const { data: a } = useQuery({
    queryKey: ["assessment", assessmentId],
    queryFn: () => api.getAssessment(assessmentId),
  });

  const [open, setOpen] = useState(false);
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["assessment", assessmentId] }),
  });

  const summary = a
    ? `Analysis date ${a.as_of_date}${a.as_of_date_set ? " (pinned)" : " (today)"} · ${
        hasStandards(a.standards_profile) ? "assessor standards set" : "no assessor standards"
      }`
    : "";

  const list = (v: string[]) => v.join("\n");
  const parseList = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
  const num = (v: number | null | undefined) => (v == null ? "" : String(v));
  const parseNum = (s: string) => (s.trim() === "" ? null : Math.max(0, parseInt(s) || 0));

  return (
    <div className="rounded-lg border border-ink-200 bg-white p-4 max-w-3xl mb-6">
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold">Assessment inputs</div>
          <div className="text-xs text-ink-600 mt-0.5">{summary}</div>
        </div>
        <button
          onClick={() => setOpen((o) => !o)}
          className="rounded border border-ink-300 text-ink-700 text-xs font-medium px-3 py-1.5 hover:bg-ink-50"
        >
          {open ? "Close" : "Edit"}
        </button>
      </div>

      {open && a && (
        <div className="mt-4 space-y-4 text-sm">
          <div>
            <label className="block text-[11px] uppercase tracking-wide text-ink-500 font-semibold mb-1">
              Analysis date
            </label>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-1.5 text-xs text-ink-700">
                <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
                Pin to a date
              </label>
              <input
                type="date"
                value={asOf}
                disabled={!pinned}
                onChange={(e) => setAsOf(e.target.value)}
                className="rounded border border-ink-200 px-2 py-1 text-xs disabled:opacity-40"
              />
            </div>
            <p className="text-[11px] text-ink-500 mt-1">
              Every prompt&apos;s &quot;Analysis date&quot; and every staleness judgement use this date (not the run
              date). Leave unpinned to use today.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Field label="Required attestations (one per line)">
              <textarea
                className="w-full rounded border border-ink-200 px-2 py-1 text-xs min-h-[60px]"
                value={list(profile.required_attestations)}
                onChange={(e) => setProfile({ ...profile, required_attestations: parseList(e.target.value) })}
                placeholder={"SOC 2 Type 2\nISO/IEC 27001"}
              />
            </Field>
            <Field label="Allowed data residency (one per line)">
              <textarea
                className="w-full rounded border border-ink-200 px-2 py-1 text-xs min-h-[60px]"
                value={list(profile.allowed_residency)}
                onChange={(e) => setProfile({ ...profile, allowed_residency: parseList(e.target.value) })}
                placeholder={"EEA\nUnited Kingdom"}
              />
            </Field>
            <NumField label="Attestation max age (months)" value={num(profile.attestation_max_age_months)}
              onChange={(v) => setProfile({ ...profile, attestation_max_age_months: parseNum(v) })} />
            <NumField label="Pen-test max age (months)" value={num(profile.pentest_max_age_months)}
              onChange={(v) => setProfile({ ...profile, pentest_max_age_months: parseNum(v) })} />
            <NumField label="Policy review cadence (months)" value={num(profile.policy_review_months)}
              onChange={(v) => setProfile({ ...profile, policy_review_months: parseNum(v) })} />
            <NumField label="Record retention target (years)" value={num(profile.retention_years)}
              onChange={(v) => setProfile({ ...profile, retention_years: parseNum(v) })} />
            <Field label="MFA policy">
              <input
                className="w-full rounded border border-ink-200 px-2 py-1 text-xs"
                value={profile.mfa_policy || ""}
                onChange={(e) => setProfile({ ...profile, mfa_policy: e.target.value || null })}
                placeholder="MFA on all accounts; phishing-resistant for privileged"
              />
            </Field>
            <Field label="Vulnerability SLA (days: critical / high / medium)">
              <div className="flex gap-2">
                {(["critical_days", "high_days", "medium_days"] as const).map((k) => (
                  <input
                    key={k}
                    type="number"
                    min={0}
                    className="w-20 rounded border border-ink-200 px-2 py-1 text-xs"
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
            </Field>
            <div className="md:col-span-2">
              <Field label="Other requirements (one per line)">
                <textarea
                  className="w-full rounded border border-ink-200 px-2 py-1 text-xs min-h-[60px]"
                  value={list(profile.other_requirements)}
                  onChange={(e) => setProfile({ ...profile, other_requirements: parseList(e.target.value) })}
                />
              </Field>
            </div>
          </div>

          <div className="flex items-center justify-end gap-3">
            {save.isError && <span className="text-xs text-risk-high">{describeError(save.error)}</span>}
            {save.isSuccess && <span className="text-xs text-emerald-700">Saved</span>}
            <button
              onClick={() => save.mutate()}
              disabled={save.isPending || (pinned && !asOf)}
              className="rounded bg-ink-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-ink-700 disabled:opacity-40"
            >
              {save.isPending ? "Saving…" : "Save inputs"}
            </button>
          </div>
          <p className="text-[11px] text-ink-500">
            Changing these does not re-run anything: stages run afterwards use the new values and the executive
            summary is flagged stale.
          </p>
        </div>
      )}
    </div>
  );
}

function hasStandards(p: StandardsProfile | undefined): boolean {
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[11px] uppercase tracking-wide text-ink-500 font-semibold mb-1">{label}</label>
      {children}
    </div>
  );
}

function NumField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={0}
        className="w-28 rounded border border-ink-200 px-2 py-1 text-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}
