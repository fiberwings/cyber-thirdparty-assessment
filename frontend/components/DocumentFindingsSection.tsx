"use client";

import { DocumentRead, WeaknessRead } from "@/lib/types";
import { SEVERITY_ORDER, SEVERITY_STYLES, severityCounts } from "@/lib/utils";
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
  searchActive,
}: {
  doc: DocumentRead;
  weaknesses: WeaknessRead[];
  defaultOpen: boolean;
  onDelete: (id: number) => void;
  searchActive: boolean;
}) {
  const counts = severityCounts(weaknesses);
  const total = weaknesses.length;
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
            {!doc.parsed_at && (
              <span className="text-[10px] uppercase tracking-wider text-amber-700 bg-amber-50 rounded px-1.5 py-0.5">
                queued
              </span>
            )}
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
