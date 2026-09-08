"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { customModelCount } from "@/lib/settings";
import type { Assessment, PhaseInfo, WorkflowKey } from "@/lib/types";
import { AiGlyph } from "./AiActivity";

// Nav items and the workflow keys each one summarises. The analysis page
// hosts three steps (correlation → gap analysis → narratives); its slot
// reflects the earliest one that is not done and current. The step number
// shown in the slot is the array index + 1.
const PHASES: { key: string; label: string; href: (id: number) => string; steps: WorkflowKey[] }[] = [
  { key: "scoping",   label: "Scoping",        href: (id) => `/assessments/${id}/scoping`,   steps: ["scoping"] },
  { key: "scenarios", label: "Inherent risk",  href: (id) => `/assessments/${id}/scenarios`, steps: ["scenarios"] },
  { key: "evidence",  label: "Evidence",       href: (id) => `/assessments/${id}/evidence`,  steps: ["evidence"] },
  { key: "analysis",  label: "Analysis",       href: (id) => `/assessments/${id}/analysis`,  steps: ["correlation", "analysis", "score"] },
  { key: "score",     label: "Residual score", href: (id) => `/assessments/${id}/score`,     steps: ["score"] },
  { key: "report",    label: "Report",         href: (id) => `/assessments/${id}/report`,    steps: ["score"] },
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

// Stage (and unit count) of the first running step, for the slot tooltip.
function runningTitle(phases: Assessment["phases"] | undefined, steps: WorkflowKey[]): string {
  const info = steps.map((k) => phases?.[k]).find((p): p is PhaseInfo => !!p && p.state === "running");
  if (!info?.stage) return "Running";
  const units = info.units_total ? ` · ${info.units_done ?? 0}/${info.units_total} ${info.unit_label ?? ""}`.trimEnd() : "";
  return `${info.stage}${units}`;
}

// The single status slot in front of each label. One glyph per row: what
// the step is doing, or its number when it has not run yet. The number is
// emphasised on the next step to run and muted on the ones after it, so a
// finished assessment shows six ticks and nothing pending.
function StepSlot({ n, badge, next, title }: { n: number; badge: Badge; next: boolean; title: string }) {
  let inner: React.ReactNode;
  switch (badge) {
    case "running":
      inner = <AiGlyph size={13} />;
      break;
    case "error":
      inner = <span className="text-[11px] font-bold text-risk-high">!</span>;
      break;
    case "stale":
      inner = <span className="text-[11px] font-bold text-amber-600">✓</span>;
      break;
    case "done":
      inner = <span className="text-[11px] font-bold text-emerald-600">✓</span>;
      break;
    default:
      inner = (
        <span className={clsx("text-[11px] tabular-nums", next ? "font-semibold text-ink-900" : "text-ink-400")}>{n}</span>
      );
  }
  return (
    <span className="w-5 shrink-0 inline-flex items-center justify-center" title={title} aria-label={title}>
      {inner}
    </span>
  );
}

function slotTitle(badge: Badge, next: boolean, phases: Assessment["phases"] | undefined, steps: WorkflowKey[]): string {
  switch (badge) {
    case "running": return runningTitle(phases, steps);
    case "error":   return "Failed";
    case "stale":   return "Done — inputs changed, re-run";
    case "done":    return "Done";
    default:        return next ? "Next step" : "Not started";
  }
}

export function LeftNav({
  assessmentId,
  vendorName,
  phases,
  modelOverrides,
}: {
  assessmentId: number;
  vendorName: string;
  phases?: Assessment["phases"];
  modelOverrides?: Record<string, string>;
}) {
  const path = usePathname();
  const { data: profiles } = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });
  const customModels = customModelCount(modelOverrides, profiles);
  const settingsHref = `/assessments/${assessmentId}/settings`;
  const settingsActive = path?.startsWith(settingsHref);

  const badges = PHASES.map((p) => badgeFor(phases, p.steps));
  // The next step to run: the first row that is not done. It is emphasised
  // only when it is plain pending — a running, failed or stale row already
  // says what needs doing, so there is never more than one call to action.
  const firstOpen = badges.findIndex((b) => b !== "done");
  const nextKey = firstOpen >= 0 && badges[firstOpen] === null ? PHASES[firstOpen].key : null;

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
        {PHASES.map((p, i) => {
          const href = p.href(assessmentId);
          const active = !!path?.startsWith(href);
          const badge = badges[i];
          const next = nextKey === p.key;
          const labelTone =
            badge === "done" ? "text-ink-600"
            : badge === null && !next ? "text-ink-500"
            : next ? "text-ink-900 font-medium"
            : "text-ink-700";
          return (
            <Link
              key={p.key}
              href={href}
              aria-current={active ? "page" : next ? "step" : undefined}
              className={clsx(
                "flex items-center gap-3 px-5 py-2.5 text-sm border-l-2 transition",
                active
                  ? "border-ink-900 bg-ink-50 text-ink-900 font-medium"
                  : clsx("border-transparent hover:bg-ink-50 hover:text-ink-900", labelTone)
              )}
            >
              <StepSlot n={i + 1} badge={badge} next={next} title={slotTitle(badge, next, phases, p.steps)} />
              <span className="flex-1">{p.label}</span>
              {badge === "stale" && (
                <span className="rounded bg-amber-100 text-amber-800 text-[9px] font-semibold uppercase tracking-wide px-1 py-px" title="Inputs changed — re-run">
                  stale
                </span>
              )}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-ink-200 py-2">
        <Link
          href={settingsHref}
          aria-current={settingsActive ? "page" : undefined}
          className={clsx(
            "flex items-center gap-3 px-5 py-2.5 text-sm border-l-2 transition",
            settingsActive
              ? "border-ink-900 bg-ink-50 text-ink-900 font-medium"
              : "border-transparent text-ink-600 hover:bg-ink-50 hover:text-ink-900"
          )}
        >
          <span className="w-5 shrink-0 inline-flex items-center justify-center">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
            </svg>
          </span>
          <span className="flex-1">Settings</span>
          {customModels > 0 && (
            <span
              className="rounded bg-amber-100 text-amber-800 text-[9px] font-semibold uppercase tracking-wide px-1 py-px"
              title={`${customModels} stage${customModels === 1 ? "" : "s"} on a non-default model`}
            >
              custom models
            </span>
          )}
        </Link>
      </div>
      <div className="px-5 py-4 border-t border-ink-200 text-[11px] text-ink-500">
        Cyber TPRM v0.1
      </div>
    </aside>
  );
}
