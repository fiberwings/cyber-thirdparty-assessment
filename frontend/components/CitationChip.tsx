"use client";

import { CitationRead } from "@/lib/types";
import { useEvidenceDrawer } from "./EvidenceDrawer";

export function CitationChip({ citation }: { citation: CitationRead }) {
  const { open } = useEvidenceDrawer();
  const label = citation.page
    ? `p.${citation.page}`
    : citation.section_path
    ? citation.section_path.slice(0, 30)
    : "ev.";
  return (
    <button
      type="button"
      onClick={() => open(citation)}
      className="inline-flex items-center gap-1 rounded-full bg-amber-50 border border-amber-300 px-2 py-0.5 text-[11px] text-amber-900 hover:bg-amber-100"
      title={citation.quote}
    >
      <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
        <path d="M2 3h8M2 6h8M2 9h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      {label}
    </button>
  );
}
