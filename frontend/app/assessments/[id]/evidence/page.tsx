"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use, useMemo, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { DocumentFindingsSection } from "@/components/DocumentFindingsSection";
import {
  TransversalFindingsSection,
  SeverityFilter,
} from "@/components/TransversalFindingsSection";
import { groupWeaknessesByDocument, matchesQuery } from "@/lib/utils";
import { WeaknessRead } from "@/lib/types";
import { useRouter } from "next/navigation";
import clsx from "clsx";

const KINDS = [
  { value: "questionnaire", label: "Questionnaire (CAIQ / SIG / custom)" },
  { value: "soc",           label: "SOC 2 / SOC 1 report" },
  { value: "iso",           label: "ISO 27001 certificate / SoA" },
  { value: "pentest",       label: "Penetration test report" },
  { value: "policy",        label: "Policy / standard / procedure" },
  { value: "other",         label: "Other" },
];

const SEVERITY_CHIPS: { value: SeverityFilter; label: string; activeBg: string }[] = [
  { value: "all",      label: "All",      activeBg: "bg-ink-900 text-white border-ink-900" },
  { value: "critical", label: "Critical", activeBg: "bg-risk-veryhigh text-white border-risk-veryhigh" },
  { value: "high",     label: "High",     activeBg: "bg-risk-high text-white border-risk-high" },
  { value: "medium",   label: "Medium",   activeBg: "bg-amber-600 text-white border-amber-600" },
  { value: "low",      label: "Low",      activeBg: "bg-risk-low text-white border-risk-low" },
];

export default function EvidencePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  const qc = useQueryClient();
  const router = useRouter();

  const { data: docs } = useQuery({
    queryKey: ["documents", aid],
    queryFn: () => api.listDocuments(aid),
  });
  const { data: weaknesses } = useQuery({
    queryKey: ["weaknesses", aid],
    queryFn: () => api.listWeaknesses(aid),
  });
  const { data: scenarios } = useQuery({
    queryKey: ["scenarios", aid],
    queryFn: () => api.listScenarios(aid),
  });

  const [kind, setKind] = useState("questionnaire");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState<SeverityFilter>("all");

  const del = useMutation({
    mutationFn: (id: number) => api.deleteDocument(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", aid] });
      qc.invalidateQueries({ queryKey: ["weaknesses", aid] });
    },
  });

  async function upload() {
    if (!file) return;
    setUploading(true);
    try {
      await api.uploadDocument(aid, kind, file);
      setFile(null);
      qc.invalidateQueries({ queryKey: ["documents", aid] });
    } finally {
      setUploading(false);
    }
  }

  const trimmedQuery = query.trim();
  const filterActive = trimmedQuery.length > 0 || severity !== "all";

  const matchesFilters = useMemo(
    () => (w: WeaknessRead) => {
      if (severity !== "all" && w.severity !== severity) return false;
      if (!trimmedQuery) return true;
      return matchesQuery(w.description, trimmedQuery) || matchesQuery(w.quote, trimmedQuery);
    },
    [trimmedQuery, severity],
  );

  const filteredWeaknesses = useMemo(
    () => (weaknesses ?? []).filter(matchesFilters),
    [weaknesses, matchesFilters],
  );

  const groups = useMemo(
    () => groupWeaknessesByDocument(filteredWeaknesses, docs ?? []),
    [filteredWeaknesses, docs],
  );

  const weaknessesById = useMemo(() => {
    const m = new Map<number, WeaknessRead>();
    for (const w of weaknesses ?? []) m.set(w.id, w);
    return m;
  }, [weaknesses]);

  const totalFindings = (weaknesses ?? []).length;
  const matchingFindings = filteredWeaknesses.length;

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Vendor evidence</h2>
      <p className="text-sm text-ink-600 mb-5 max-w-3xl">
        Upload the questionnaire and supporting evidence (SOC 2, ISO 27001 SoA, pen test, policies). PDFs, XLSX, and
        DOCX are supported. Each document is parsed into citation-ready chunks, and findings extracted from it appear
        below alongside any cross-document patterns.
      </p>

      <div className="rounded-lg border border-ink-200 bg-white p-4 max-w-3xl mb-6">
        <div className="grid grid-cols-1 md:grid-cols-[1fr_2fr_auto] gap-3">
          <select
            className="rounded border border-ink-200 px-2 py-2 text-sm"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
          <input
            type="file"
            accept=".pdf,.xlsx,.xlsm,.docx,.txt,.csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="text-sm"
          />
          <button
            onClick={upload}
            disabled={!file || uploading}
            className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
      </div>

      {(docs?.length ?? 0) > 0 && (
        <div className="mb-5 max-w-3xl flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="relative w-full sm:w-72">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search findings…"
              className="w-full rounded border border-ink-200 bg-white pl-8 pr-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ink-300"
            />
            <svg
              className="absolute left-2.5 top-2.5 text-ink-400"
              width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden
            >
              <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5" />
              <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            {SEVERITY_CHIPS.map((c) => {
              const active = severity === c.value;
              return (
                <button
                  key={c.value}
                  type="button"
                  onClick={() => setSeverity(c.value)}
                  className={clsx(
                    "rounded-full border text-xs font-medium px-2.5 py-1 transition-colors",
                    active
                      ? c.activeBg
                      : "border-ink-200 text-ink-600 hover:bg-ink-50",
                  )}
                >
                  {c.label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {filterActive && (docs?.length ?? 0) > 0 && (
        <div className="mb-4 text-xs text-ink-500">
          Showing <strong className="text-ink-700 font-semibold">{matchingFindings}</strong> of {totalFindings} findings
          {trimmedQuery && <> matching &ldquo;{trimmedQuery}&rdquo;</>}
          {severity !== "all" && <> · severity: <strong className="text-ink-700 font-semibold">{severity}</strong></>}
          {" "}
          <button
            type="button"
            onClick={() => { setQuery(""); setSeverity("all"); }}
            className="ml-2 underline hover:text-ink-800"
          >
            clear
          </button>
        </div>
      )}

      {!docs || docs.length === 0 ? (
        <div className="text-sm text-ink-500 p-6 rounded border border-dashed border-ink-200 text-center max-w-3xl">
          No documents yet. Upload at least one to begin extraction.
        </div>
      ) : (
        <div className="space-y-6 max-w-3xl">
          <section>
            <div className="flex items-baseline justify-between mb-3">
              <h3 className="text-sm font-semibold text-ink-900">
                Per-document findings
                <span className="ml-2 text-xs font-normal text-ink-500">
                  ({docs.length} document{docs.length === 1 ? "" : "s"})
                </span>
              </h3>
            </div>
            <div className="space-y-2.5">
              {groups.map((g) => (
                <DocumentFindingsSection
                  key={g.doc.id}
                  doc={g.doc}
                  weaknesses={g.weaknesses}
                  defaultOpen={false}
                  searchActive={filterActive}
                  onDelete={(id) => del.mutate(id)}
                />
              ))}
            </div>
          </section>

          {scenarios && weaknesses && (
            <TransversalFindingsSection
              scenarios={scenarios}
              weaknessesById={weaknessesById}
              query={trimmedQuery}
              severity={severity}
            />
          )}
        </div>
      )}

      <div className="mt-8 flex justify-end max-w-3xl">
        <button
          onClick={() => router.push(`/assessments/${aid}/analysis`)}
          className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700"
        >
          Continue to gap analysis →
        </button>
      </div>
    </AssessmentShell>
  );
}
