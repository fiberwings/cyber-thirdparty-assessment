"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import type { Assessment, PhaseInfo, WorkflowKey } from "@/lib/types";

// Nav items and the workflow keys each one summarises. The analysis page
// hosts three steps (correlation → gap analysis → narratives); its badge
// reflects the earliest one that is not done and current.
const PHASES: { key: string; label: string; href: (id: number) => string; steps: WorkflowKey[] }[] = [
  { key: "scoping",   label: "1. Scoping",          href: (id) => `/assessments/${id}/scoping`,   steps: ["scoping"] },
  { key: "scenarios", label: "2. Inherent risk",    href: (id) => `/assessments/${id}/scenarios`, steps: ["scenarios"] },
  { key: "evidence",  label: "3. Evidence",         href: (id) => `/assessments/${id}/evidence`,  steps: ["evidence"] },
  { key: "analysis",  label: "4. Analysis",         href: (id) => `/assessments/${id}/analysis`,  steps: ["correlation", "analysis", "score"] },
  { key: "score",     label: "5. Residual score",   href: (id) => `/assessments/${id}/score`,     steps: ["score"] },
  { key: "report",    label: "6. Report",           href: (id) => `/assessments/${id}/report`,    steps: ["score"] },
];

type Badge = "done" | "running" | "error" | "stale" | null;

function badgeFor(phases: Assessment["phases"] | undefined, steps: WorkflowKey[]): Badge {
  if (!phases) return null;
  const infos = steps.map((k) => phases[k]).filter((p): p is PhaseInfo => !!p);
  if (infos.length === 0) return null;
  for (const info of infos) {
    if (info.state === "running") return "running";
    if (info.state === "error") return "error";
    if (info.state === "done" && info.stale) return "stale";
    if (info.state !== "done") return null;
  }
  return "done";
}

function NavBadge({ badge }: { badge: Badge }) {
  switch (badge) {
    case "done":
      return <span className="text-[11px] font-bold text-emerald-600" title="Done">✓</span>;
    case "running":
      return <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" title="Running" />;
    case "error":
      return <span className="text-[11px] font-bold text-risk-high" title="Failed">!</span>;
    case "stale":
      return (
        <span className="rounded bg-amber-100 text-amber-800 text-[9px] font-semibold uppercase tracking-wide px-1 py-px" title="Inputs changed — re-run">
          stale
        </span>
      );
    default:
      return null;
  }
}

export function LeftNav({
  assessmentId,
  vendorName,
  currentPhase,
  phases,
}: {
  assessmentId: number;
  vendorName: string;
  currentPhase?: string;
  phases?: Assessment["phases"];
}) {
  const path = usePathname();
  return (
    <aside className="w-64 shrink-0 border-r border-ink-200 bg-white h-screen sticky top-0 flex flex-col">
      <div className="px-5 py-5 border-b border-ink-200">
        <Link href="/" className="text-xs uppercase tracking-wide text-ink-500 hover:text-ink-700">
          ← All assessments
        </Link>
        <h1 className="mt-2 font-semibold text-base text-ink-900 truncate" title={vendorName}>
          {vendorName}
        </h1>
        <div className="mt-1 text-xs text-ink-500">Assessment #{assessmentId}</div>
      </div>
      <nav className="flex-1 overflow-y-auto py-3">
        {PHASES.map((p) => {
          const href = p.href(assessmentId);
          const active = path?.startsWith(href);
          const isCurrent = currentPhase === p.key;
          const badge = badgeFor(phases, p.steps);
          return (
            <Link
              key={p.key}
              href={href}
              className={clsx(
                "flex items-center gap-3 px-5 py-2.5 text-sm border-l-2 transition",
                active
                  ? "border-ink-900 bg-ink-50 text-ink-900 font-medium"
                  : "border-transparent text-ink-600 hover:bg-ink-50 hover:text-ink-900"
              )}
            >
              <span className="flex-1">{p.label}</span>
              <NavBadge badge={badge} />
              {isCurrent && !active && badge !== "running" && <span className="h-1.5 w-1.5 rounded-full bg-ink-900" />}
            </Link>
          );
        })}
      </nav>
      <div className="px-5 py-4 border-t border-ink-200 text-[11px] text-ink-500">
        Cyber TPRM v0.1
      </div>
    </aside>
  );
}
