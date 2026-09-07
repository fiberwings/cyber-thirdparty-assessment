"use client";

import { DocumentRead, WeaknessRead } from "@/lib/types";
import { SEVERITY_ORDER, SEVERITY_STYLES, severityCounts } from "@/lib/utils";
import { useTask } from "@/lib/useTask";
import { AiActivity } from "./AiActivity";
import { FindingRow } from "./FindingRow";
import clsx from "clsx";

const SEV_LABEL: Record<WeaknessRead["severity"], string> = {
  critical: "C",
  high: "H",
  medium: "M",
  low: "L",
};

export function DocumentFindingsSection({
  doc,
  weaknesses,
  defaultOpen,
  onDelete,
  onRetryExtraction,
  onRerunProfile,
  profileRunningSince,
  searchActive,
}: {
  doc: DocumentRead;
  weaknesses: WeaknessRead[];
  defaultOpen: boolean;
  onDelete: (id: number) => void;
  onRetryExtraction?: (id: number) => void;
  onRerunProfile?: (id: number) => void;
  // Client clock (ms) when a synchronous profile re-run started for this
  // document; null when none is in flight.
  profileRunningSince?: number | null;
  searchActive: boolean;
}) {
  // Header counts reflect reported rows only; the list still shows
  // candidates, evidence notes, dropped and merged rows with their status.
  const reported = weaknesses.filter((w) => (w.status ?? "confirmed") === "confirmed");
  const counts = severityCounts(reported);
  const total = reported.length;
  const notReported = weaknesses.length - total;
  const empty = total === 0;
  // When a search is active, force-open sections that have matches and
  // force-close sections that don't, so the filter result is obvious.
  const open = searchActive ? !empty : defaultOpen;

  return (
    <details
      key={`${doc.id}-${searchActive}-${defaultOpen}`}
      open={open}
      className={clsx(
        "group rounded-lg border bg-white",
        empty ? "border-ink-100 opacity-70" : "border-ink-200",
      )}
    >
      {/* rendered below the summary row */}
      <summary
        className={clsx(
          "flex items-center gap-3 px-4 py-3 cursor-pointer select-none list-none",
          "[&::-webkit-details-marker]:hidden",
        )}
      >
        <Caret />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-ink-900 truncate">{doc.filename}</span>
            <span className="text-[10px] uppercase tracking-wider text-ink-500 bg-ink-100 rounded px-1.5 py-0.5">
              {doc.kind}
            </span>
            {notReported > 0 && (
              <span
                className="text-[10px] uppercase tracking-wider text-ink-500 bg-ink-50 border border-ink-200 rounded px-1.5 py-0.5"
                title="Candidates the bundle-aware review did not report (dropped, evidence notes, merged)"
              >
                {notReported} not reported
              </span>
            )}
            {!doc.parsed_at && (
              <span className="text-[10px] uppercase tracking-wider text-amber-700 bg-amber-50 rounded px-1.5 py-0.5">
                queued
              </span>
            )}
            <ExtractionChip doc={doc} />
          </div>
          <div className="text-[11px] text-ink-500 mt-0.5">
            {(doc.size_bytes / 1024).toFixed(1)} KB
            {" · "}
            {empty ? (
              "no findings"
            ) : (
              <>
                <strong className="text-ink-700 font-semibold">{total}</strong>
                {" finding"}{total === 1 ? "" : "s"}
                {" · "}
                <SeverityCounts counts={counts} />
              </>
            )}
          </div>
        </div>
        {doc.extraction_state === "error" && onRetryExtraction && (
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onRetryExtraction(doc.id);
            }}
            className="text-[11px] text-amber-700 hover:text-amber-900 shrink-0 font-medium"
          >
            retry extraction
          </button>
        )}
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onDelete(doc.id);
          }}
          className="text-[11px] text-rose-600 hover:text-rose-800 shrink-0"
        >
          delete
        </button>
      </summary>
      {doc.extraction_state === "error" && doc.weakness_error && (
        <div className="mx-4 mb-3 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
          {doc.weakness_error}
        </div>
      )}
      {doc.attestation_profile_error && !doc.attestation_profile && (
        <div className="mx-4 mb-3 flex items-start gap-3 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
          <div className="min-w-0 flex-1">
            <span className="font-semibold">Attestation profile not extracted</span> — the deterministic
            freshness / scope / opinion checks did not run for this document (it still counts as supplied).
            <div className="mt-0.5 break-words text-amber-700/90">{doc.attestation_profile_error}</div>
          </div>
          {profileRunningSince != null ? (
            <AiActivity
              variant="inline"
              kind="attestation_profile"
              startedAtMs={profileRunningSince}
              className="w-56 shrink-0"
            />
          ) : onRerunProfile && (
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onRerunProfile(doc.id);
              }}
              className="shrink-0 font-medium text-amber-700 hover:text-amber-900"
            >
              re-run profile
            </button>
          )}
        </div>
      )}
      {doc.attestation_profile && <AttestationProfileCard profile={doc.attestation_profile} />}

      {!empty && (
        <ul className="border-t border-ink-100 text-ink-700">
          {weaknesses.map((w) => (
            <FindingRow key={w.id} weakness={w} documentId={doc.id} />
          ))}
        </ul>
      )}
    </details>
  );
}

// Per-document weakness extraction state — the evidence step is done only
// when every document is "done".
function ExtractionChip({ doc }: { doc: DocumentRead }) {
  const live = doc.extraction_state === "running" || doc.extraction_state === "pending";
  // Attach to the document's own extraction task for stage + window counts.
  const { task } = useTask(live ? doc.weakness_task_id : null);
  switch (doc.extraction_state) {
    case "running":
    case "pending":
      return <AiActivity variant="chip" kind="document_extraction" source={task} />;
    case "error":
      return (
        <span className="text-[10px] uppercase tracking-wider text-white bg-risk-high rounded px-1.5 py-0.5" title={doc.weakness_error ?? undefined}>
          extraction failed
        </span>
      );
    default:
      return null;
  }
}

function SeverityCounts({ counts }: { counts: Record<WeaknessRead["severity"], number> }) {
  const visible = SEVERITY_ORDER.filter((s) => counts[s] > 0);
  if (visible.length === 0) return <span className="text-ink-400">no findings</span>;
  return (
    <span className="inline-flex items-center gap-1.5 font-mono">
      {visible.map((s) => (
        <span key={s} className={clsx("tabular-nums", SEVERITY_STYLES[s].text)}>
          {counts[s]}{SEV_LABEL[s]}
        </span>
      ))}
    </span>
  );
}

function Caret() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      className="shrink-0 text-ink-400 transition-transform group-open:rotate-90"
      aria-hidden
    >
      <path d="M3 1l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </svg>
  );
}


function AttestationProfileCard({ profile }: { profile: Record<string, any> }) {
  const q = (f: any) => (f && f.value !== undefined ? String(f.value) : null);
  const items: [string, string | null][] = [
    ["Type", profile.doc_type ?? null],
    ["Period", q(profile.period_start) && q(profile.period_end) ? `${q(profile.period_start)} → ${q(profile.period_end)}` : null],
    ["Opinion", q(profile.opinion)],
    ["First examination", profile.first_examination ? String(profile.first_examination.value) : null],
    ["Carve-outs", profile.carve_outs?.length ? profile.carve_outs.map((c: any) => c.name).join(", ") : null],
    ["CUECs", q(profile.cuec_count)],
    ["Auditor / tester", q(profile.auditor) ?? q(profile.tester)],
    ["Cert expiry", q(profile.cert_expiry_date)],
    ["Test date", q(profile.test_end_date) ?? q(profile.test_start_date)],
  ];
  const shown = items.filter(([, v]) => v);
  if (!shown.length) return null;
  return (
    <div className="mx-4 mb-2 rounded border border-ink-100 bg-ink-50/50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">
        Attestation profile <span className="normal-case font-normal">(every value carries a source quote; drives deterministic freshness checks)</span>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-ink-700">
        {shown.map(([k, v]) => (
          <span key={k}>
            <span className="text-ink-500">{k}:</span> {v}
          </span>
        ))}
      </div>
    </div>
  );
}
