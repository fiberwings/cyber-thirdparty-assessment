"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { phaseDotColor, phaseList, phaseSummary } from "@/lib/utils";
import type { Assessment } from "@/lib/types";

export default function Home() {
  const qc = useQueryClient();
  const router = useRouter();
  const { data, isLoading } = useQuery({
    queryKey: ["assessments"],
    queryFn: () => api.listAssessments(),
    // Refetch periodically so running phases update without a manual reload.
    refetchInterval: (q) => {
      const rows = q.state.data as Assessment[] | undefined;
      const anyRunning = rows?.some((a) =>
        Object.values(a.phases ?? {}).some((p) => p?.state === "running"),
      );
      return anyRunning ? 2000 : false;
    },
  });
  const [vendor, setVendor] = useState("");
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteAssessment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["assessments"] }),
  });
  const create = useMutation({
    mutationFn: (name: string) => api.createAssessment(name),
    onSuccess: (a) => {
      qc.invalidateQueries({ queryKey: ["assessments"] });
      router.push(`/assessments/${a.id}/scoping`);
    },
  });

  return (
    <div className="min-h-screen flex flex-col">
      <header className="px-8 py-6 border-b border-ink-200 bg-white flex items-center justify-between">
        <div>
          <div className="text-xs uppercase tracking-wide text-ink-500">Cyber TPRM</div>
          <h1 className="text-2xl font-bold text-ink-900">Third-party risk assessments</h1>
        </div>
      </header>

      <div className="flex-1 max-w-5xl w-full mx-auto px-8 py-8">
        <div className="rounded-lg border border-ink-200 bg-white p-5">
          <div className="text-sm font-semibold text-ink-900">Start a new assessment</div>
          <p className="text-xs text-ink-500 mt-1">
            Enter the vendor name. Next, you&apos;ll describe the service so the AI can scope inherent risk.
          </p>
          <form
            className="mt-4 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (vendor.trim()) create.mutate(vendor.trim());
            }}
          >
            <input
              className="flex-1 rounded border border-ink-200 px-3 py-2 text-sm"
              placeholder="e.g. Acme Billing"
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
            />
            <button
              type="submit"
              disabled={create.isPending || !vendor.trim()}
              className="rounded bg-ink-900 text-white text-sm font-medium px-4 py-2 hover:bg-ink-700 disabled:opacity-40"
            >
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </form>
        </div>

        <div className="mt-8">
          <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-3">Recent assessments</div>
          {isLoading ? (
            <div className="text-sm text-ink-500">Loading…</div>
          ) : data && data.length > 0 ? (
            <ul className="rounded-lg border border-ink-200 bg-white divide-y divide-ink-100">
              {data.map((a) => (
                <li key={a.id} className="flex items-center gap-2 pr-3 hover:bg-ink-50">
                  <Link href={`/assessments/${a.id}/scoping`} className="flex items-center justify-between gap-4 px-5 py-3 min-w-0 flex-1">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-ink-900">
                        {a.vendor_name} <span className="text-ink-400 font-normal">#{a.id}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-1.5">
                        <div className="flex items-center gap-1" aria-label="Phase tracker">
                          {phaseList(a).map(({ key, info }) => (
                            <span
                              key={key}
                              title={`${key}: ${info.state}`}
                              className={`h-2 w-2 rounded-full ${phaseDotColor(info.state)}`}
                            />
                          ))}
                        </div>
                        <span className="text-xs text-ink-600 truncate">{phaseSummary(a)}</span>
                      </div>
                    </div>
                    <div className="text-xs text-ink-500 shrink-0">
                      {new Date(a.created_at).toLocaleString()}
                    </div>
                  </Link>
                  <button
                    type="button"
                    title="Delete assessment"
                    aria-label={`Delete assessment ${a.vendor_name} #${a.id}`}
                    disabled={remove.isPending}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete assessment "${a.vendor_name}" #${a.id}? This removes its documents, scenarios, findings, and scores permanently.`,
                        )
                      ) {
                        remove.mutate(a.id);
                      }
                    }}
                    className="shrink-0 rounded px-2 py-1 text-xs text-ink-400 hover:text-red-600 hover:bg-red-50 disabled:opacity-40"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-ink-500 p-4 rounded border border-dashed border-ink-200 text-center">
              No assessments yet.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
