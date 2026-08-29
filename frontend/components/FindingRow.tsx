"use client";

import { EvidenceRef, WeaknessRead } from "@/lib/types";
import { SeverityBadge } from "./SeverityBadge";
import { useEvidenceDrawer } from "./EvidenceDrawer";

export function FindingRow({ weakness, documentId }: { weakness: WeaknessRead; documentId?: number | null }) {
  const { open } = useEvidenceDrawer();
  // The evidence drawer keys on chunk_id; document_id is used to load the
  // signed URL header. Both come from the weakness; fall back to the prop
  // for transversal sections that pass the parent doc explicitly.
  const docId = weakness.source_document_id ?? documentId ?? 0;
  const chunkId = weakness.source_chunk_id ?? 0;
  const canOpen = chunkId > 0 && (weakness.evidence_refs ?? []).length <= 1;

  const handle = () => {
    if (!canOpen) return;
    open({
      document_id: docId,
      chunk_id: chunkId,
      page: null,
      section_path: "",
      quote: weakness.quote,
      polarity: "contradicts",
    });
  };

  const kindLabel =
    weakness.kind_signal === "cross_doc_conflict"
      ? "cross-document conflict"
      : weakness.kind_signal
        ? weakness.kind_signal.replace(/_/g, " ")
        : "";
  // Contradictions carry one evidence ref per disagreeing side; render each
  // as its own clickable quote instead of the single headline quote.
  const refs = (weakness.evidence_refs ?? []).length > 1 ? weakness.evidence_refs : null;

  const openRef = (ref: EvidenceRef) => {
    if (!ref.chunk_id) return;
    open({
      document_id: ref.document_id,
      chunk_id: ref.chunk_id,
      page: ref.page,
      section_path: ref.section_path,
      quote: ref.quote,
      polarity: "contradicts",
    });
  };

  return (
    <li
      className={`group flex gap-3 px-4 py-3 border-t border-ink-100 first:border-t-0 ${
        canOpen ? "cursor-pointer hover:bg-ink-50" : ""
      }`}
      onClick={refs ? undefined : handle}
    >
      <SeverityBadge severity={weakness.severity} className="mt-0.5 shrink-0 w-[68px]" />
      <div className="min-w-0 flex-1">
        <div className="text-sm text-ink-800 leading-snug">{weakness.description}</div>
        {refs ? (
          <ul className="mt-1 space-y-0.5">
            {refs.map((ref, i) => (
              <li
                key={i}
                className={`text-[11px] text-ink-500 italic line-clamp-2 ${
                  ref.chunk_id ? "cursor-pointer hover:text-ink-800" : ""
                }`}
                onClick={(e) => {
                  e.stopPropagation();
                  openRef(ref);
                }}
              >
                <span className="not-italic text-ink-400">
                  {ref.section_path || (ref.page != null ? `p.${ref.page}` : "source")}:
                </span>{" "}
                &ldquo;{ref.quote}&rdquo;
              </li>
            ))}
          </ul>
        ) : (
          weakness.quote && (
            <div className="mt-1 text-[11px] text-ink-500 italic line-clamp-2">
              &ldquo;{weakness.quote}&rdquo;
            </div>
          )
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {kindLabel && (
            <span className="rounded-full border border-ink-200 bg-ink-50 text-[10px] uppercase tracking-wide text-ink-600 px-1.5 py-0.5">
              {kindLabel}
            </span>
          )}
          {weakness.mapped_control_codes.map((code) => (
            <span
              key={code}
              className="rounded-full border border-ink-200 bg-white text-[10px] text-ink-600 px-1.5 py-0.5 font-mono"
            >
              {code}
            </span>
          ))}
          {weakness.user_edited && (
            <span className="rounded-full bg-emerald-50 border border-emerald-200 text-[10px] text-emerald-700 px-1.5 py-0.5">
              edited
            </span>
          )}
        </div>
      </div>
      {canOpen && (
        <span className="text-ink-300 group-hover:text-ink-500 text-xs self-center shrink-0">→</span>
      )}
    </li>
  );
}
