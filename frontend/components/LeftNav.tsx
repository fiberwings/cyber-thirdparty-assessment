"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";

const PHASES: { key: string; label: string; href: (id: number) => string }[] = [
  { key: "scoping",   label: "1. Scoping",          href: (id) => `/assessments/${id}/scoping` },
  { key: "scenarios", label: "2. Inherent risk",    href: (id) => `/assessments/${id}/scenarios` },
  { key: "evidence",  label: "3. Evidence",         href: (id) => `/assessments/${id}/evidence` },
  { key: "analysis",  label: "4. Gap analysis",     href: (id) => `/assessments/${id}/analysis` },
  { key: "score",     label: "5. Residual score",   href: (id) => `/assessments/${id}/score` },
  { key: "report",    label: "6. Report",           href: (id) => `/assessments/${id}/report` },
];

export function LeftNav({ assessmentId, vendorName, currentPhase }: { assessmentId: number; vendorName: string; currentPhase?: string }) {
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
              {isCurrent && !active && <span className="h-1.5 w-1.5 rounded-full bg-ink-900" />}
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
