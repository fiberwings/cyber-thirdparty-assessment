"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { use, useState } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { useRouter } from "next/navigation";

const KINDS = [
  { value: "questionnaire", label: "Questionnaire (CAIQ / SIG / custom)" },
  { value: "soc",           label: "SOC 2 / SOC 1 report" },
  { value: "iso",           label: "ISO 27001 certificate / SoA" },
  { value: "pentest",       label: "Penetration test report" },
  { value: "policy",        label: "Policy / standard / procedure" },
  { value: "other",         label: "Other" },
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

  const [kind, setKind] = useState("questionnaire");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);

  const del = useMutation({
    mutationFn: (id: number) => api.deleteDocument(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["documents", aid] }),
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

  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Vendor evidence</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Upload the questionnaire and supporting evidence (SOC 2, ISO 27001 SoA, pen test, policies). PDFs, XLSX, and
        DOCX are supported. Each document is parsed into citation-ready chunks.
      </p>

      <div className="rounded-lg border border-ink-200 bg-white p-5 max-w-3xl">
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

      <div className="mt-6">
        <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Uploaded documents</div>
        {!docs || docs.length === 0 ? (
          <div className="text-sm text-ink-500 p-4 rounded border border-dashed border-ink-200 text-center max-w-3xl">
            No documents yet.
          </div>
        ) : (
          <div className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100 max-w-3xl">
            {docs.map((d) => (
              <div key={d.id} className="flex items-center justify-between px-5 py-3">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-ink-900 truncate">{d.filename}</div>
                  <div className="text-xs text-ink-500 mt-0.5">
                    <span className="uppercase tracking-wide">{d.kind}</span> · {(d.size_bytes / 1024).toFixed(1)} KB · {d.parsed_at ? "parsed" : "queued"}
                  </div>
                </div>
                <button
                  onClick={() => del.mutate(d.id)}
                  className="text-xs text-rose-600 hover:text-rose-800"
                >
                  delete
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

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
