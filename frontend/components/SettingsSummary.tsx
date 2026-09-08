"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { settingsSummary } from "@/lib/settings";

// Read-only digest of the assessment's settings with a link to edit them.
// Shown on the scoping page so the inputs stay discoverable at the start.
export function SettingsSummary({ assessmentId }: { assessmentId: number }) {
  const { data: a } = useQuery({ queryKey: ["assessment", assessmentId], queryFn: () => api.getAssessment(assessmentId) });
  const { data: profiles } = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });
  if (!a) return null;
  return (
    <div className="rounded-lg border border-ink-200 bg-white px-4 py-2.5 max-w-3xl mb-6 flex items-center justify-between gap-4">
      <div className="min-w-0">
        <div className="text-xs uppercase tracking-wide text-ink-500 font-semibold">Settings</div>
        <div className="text-xs text-ink-600 mt-0.5 truncate">{settingsSummary(a, profiles)}</div>
      </div>
      <Link
        href={`/assessments/${assessmentId}/settings`}
        className="shrink-0 rounded border border-ink-300 text-ink-700 text-xs font-medium px-3 py-1.5 hover:bg-ink-50"
      >
        Edit settings →
      </Link>
    </div>
  );
}
