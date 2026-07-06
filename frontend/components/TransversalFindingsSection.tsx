"use client";

import { ScenarioRead, WeaknessRead } from "@/lib/types";
import {
  bandColor,
  bandLabel,
  compareScenariosByRisk,
  compareWeaknessesBySeverity,
  matchesQuery,
} from "@/lib/utils";
import { FindingRow } from "./FindingRow";
import clsx from "clsx";

export type SeverityFilter = "all" | WeaknessRead["severity"];

function passes(w: WeaknessRead, query: string, severity: SeverityFilter): boolean {
  if (severity !== "all" && w.severity !== severity) return false;
  if (!query) return true;
  return matchesQuery(w.description, query) || matchesQuery(w.quote, query);
}

export function TransversalFindingsSection({
  scenarios,
  weaknessesById,
  query,
  severity,
}: {
  scenarios: ScenarioRead[];
  weaknessesById: Map<number, WeaknessRead>;
  query: string;
  severity: SeverityFilter;
}) {
  const emergent = scenarios
    .filter((s) => s.source === "emergent_from_weakness")
    .slice()
    .sort(compareScenariosByRisk);

  if (emergent.length === 0) return null;

  const filterActive = query.trim().length > 0 || severity !== "all";

  // A card is visible if (a) no filter is active, (b) the scenario text
  // matches the query, or (c) at least one underlying finding passes.
  const visible = emergent.filter((s) => {
    if (!filterActive) return true;
    const refs = (s.origin_weakness_ids ?? [])
      .map((id) => weaknessesById.get(id))
      .filter((w): w is WeaknessRead => !!w);
    if (refs.some((w) => passes(w, query, severity))) return true;
    if (severity !== "all") return false;
    // Severity is 'all' but query is set: also let the scenario's own text match.
    return matchesQuery(`${s.name} ${s.description} ${s.code}`, query);
  });

  return (
    <section>
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-sm font-semibold text-ink-900">
          Cross-document findings
          <span className="ml-2 text-xs font-normal text-ink-500">
            ({emergent.length})
          </span>
        </h3>
        <span className="text-[11px] text-ink-500">
          patterns synthesised across the evidence
        </span>
      </div>
      {visible.length === 0 ? (
        <div className="rounded-lg border border-dashed border-ink-200 bg-white p-4 text-xs text-ink-500">
          No cross-document findings match the current filter.
        </div>
      ) : (
        <div className="space-y-3">
          {visible.map((s) => (
            <EmergentCard
              key={s.id}
              scenario={s}
              weaknessesById={weaknessesById}
              query={query}
              severity={severity}
              filterActive={filterActive}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function EmergentCard({
  scenario,
  weaknessesById,
  query,
  severity,
  filterActive,
}: {
  scenario: ScenarioRead;
  weaknessesById: Map<number, WeaknessRead>;
  query: string;
  severity: SeverityFilter;
  filterActive: boolean;
}) {
  const refs: WeaknessRead[] = (scenario.origin_weakness_ids ?? [])
    .map((id) => weaknessesById.get(id))
    .filter((w): w is WeaknessRead => !!w);

  const filteredRefs = filterActive ? refs.filter((w) => passes(w, query, severity)) : refs;
  const sortedRefs = filteredRefs.slice().sort(compareWeaknessesBySeverity);
  const open = filterActive && sortedRefs.length > 0;

  return (
    <details
      open={open || undefined}
      className="group rounded-lg border border-ink-200 bg-white overflow-hidden"
    >
      <summary
        className={clsx(
          "flex items-start gap-3 px-4 py-3 cursor-pointer select-none list-none",
          "[&::-webkit-details-marker]:hidden",
        )}
      >
        <Caret />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500 bg-ink-100 rounded px-1.5 py-0.5">
              {scenario.code}
            </span>
            <span className="text-sm font-semibold text-ink-900">{scenario.name}</span>
          </div>
          <div className="mt-1 text-xs text-ink-600 leading-snug line-clamp-3">
            {scenario.description}
          </div>
          <div className="mt-1.5 text-[11px] text-ink-500">
            {refs.length > 0 ? (
              <>
                Drawn from <strong className="text-ink-700 font-semibold">{refs.length}</strong>{" "}
                underlying finding{refs.length === 1 ? "" : "s"}
                {filterActive && filteredRefs.length !== refs.length && (
                  <span className="text-ink-400"> · {filteredRefs.length} match filter</span>
                )}
              </>
            ) : (
              "No underlying findings linked"
            )}
          </div>
        </div>
        <div
          className={clsx(
            "shrink-0 rounded-md text-white text-[11px] font-semibold px-2 py-1",
            bandColor(scenario.score_band),
          )}
        >
          {bandLabel(scenario.score_band)}
        </div>
      </summary>

      {sortedRefs.length > 0 && (
        <ul className="border-t border-ink-100 text-ink-700">
          {sortedRefs.map((w) => (
            <FindingRow key={w.id} weakness={w} />
          ))}
        </ul>
      )}
    </details>
  );
}

function Caret() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      className="mt-1.5 shrink-0 text-ink-400 transition-transform group-open:rotate-90"
      aria-hidden
    >
      <path d="M3 1l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </svg>
  );
}
