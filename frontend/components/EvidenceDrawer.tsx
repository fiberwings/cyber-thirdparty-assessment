"use client";

import { createContext, useContext, useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { CitationRead } from "@/lib/types";

type DrawerState = {
  citation?: CitationRead;
  open: boolean;
};

const Ctx = createContext<{
  open: (cite: CitationRead) => void;
  close: () => void;
}>({ open: () => {}, close: () => {} });

export function useEvidenceDrawer() {
  return useContext(Ctx);
}

export function EvidenceDrawerProvider({ assessmentId, children }: { assessmentId: number; children: React.ReactNode }) {
  const [state, setState] = useState<DrawerState>({ open: false });
  const open = useCallback((c: CitationRead) => setState({ open: true, citation: c }), []);
  const close = useCallback(() => setState({ open: false }), []);

  return (
    <Ctx.Provider value={{ open, close }}>
      {children}
      <Drawer state={state} onClose={close} />
    </Ctx.Provider>
  );
}

function Drawer({ state, onClose }: { state: DrawerState; onClose: () => void }) {
  const c = state.citation;
  const { data: chunk } = useQuery({
    enabled: !!c?.chunk_id,
    queryKey: ["chunk", c?.chunk_id],
    queryFn: () => api.getChunk(c!.chunk_id),
  });
  const { data: docUrl } = useQuery({
    enabled: !!c?.document_id,
    queryKey: ["docurl", c?.document_id],
    queryFn: () => api.documentSignedUrl(c!.document_id),
  });

  return (
    <>
      {state.open && (
        <div
          className="fixed inset-0 bg-ink-950/30 z-40"
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className={`fixed top-0 right-0 h-full w-[480px] bg-white border-l border-ink-200 shadow-2xl z-50 transition-transform duration-200 ${
          state.open ? "translate-x-0" : "translate-x-full"
        } flex flex-col`}
      >
        <header className="px-5 py-4 border-b border-ink-200 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wide text-ink-500">Evidence</div>
            <div className="text-sm font-semibold text-ink-900">
              {docUrl?.filename || "—"}
              {c?.page ? <span className="ml-2 text-ink-500 font-normal">page {c.page}</span> : null}
            </div>
            {c?.section_path && (
              <div className="text-xs text-ink-500 mt-0.5">{c.section_path}</div>
            )}
          </div>
          <button onClick={onClose} className="text-ink-500 hover:text-ink-900 text-xl leading-none">×</button>
        </header>

        <div className="flex-1 overflow-y-auto p-5">
          <div className="rounded-md border-l-2 border-amber-400 bg-amber-50 p-3 text-sm text-ink-800">
            {c?.quote || "(no quote)"}
          </div>

          <div className="mt-5">
            <div className="text-xs uppercase tracking-wide text-ink-500 mb-2">Context</div>
            <div className="rounded-md border border-ink-200 bg-ink-50 p-3 text-sm text-ink-800 whitespace-pre-wrap font-mono text-[12px]">
              {chunk?.text || "Loading…"}
            </div>
          </div>

          {docUrl?.url && (
            <a
              href={docUrl.url}
              target="_blank"
              rel="noreferrer"
              className="mt-5 inline-flex items-center text-xs text-ink-600 hover:text-ink-900 underline"
            >
              Open full document →
            </a>
          )}
        </div>
      </aside>
    </>
  );
}
