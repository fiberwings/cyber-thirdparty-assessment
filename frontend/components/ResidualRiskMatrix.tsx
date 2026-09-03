"use client";

import { useMemo, useState } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { ArrowRight, Eye, EyeOff } from "lucide-react";
import clsx from "clsx";
import { ScenarioScoreRead } from "@/lib/types";
import { BAND_ORDER, bandColor, bandLabel, bandTint, compareScenarioScoresByRisk } from "@/lib/utils";
import { cellKey } from "@/lib/riskMatrix";
import { Ranked, RiskGrid, hasMoved, scenarioAriaLabel } from "@/components/RiskGrid";

export function ResidualRiskMatrix({
  scenarios,
  onSelect,
}: {
  scenarios: ScenarioScoreRead[];
  onSelect: (code: string) => void;
}) {
  const [activeCode, setActiveCode] = useState<string | null>(null);
  const [showInherent, setShowInherent] = useState(false);
  const [expandedCells, setExpandedCells] = useState<Set<string>>(() => new Set());

  const ranked = useMemo<Ranked[]>(
    () => [...scenarios].sort(compareScenarioScoresByRisk).map((s, i) => ({ s, rank: i + 1 })),
    [scenarios],
  );

  const { byResidualCell, byInherentCell } = useMemo(() => {
    const byResidualCell = new Map<string, Ranked[]>();
    const byInherentCell = new Map<string, Ranked[]>();
    for (const r of ranked) {
      const rk = cellKey(r.s.residual_impact, r.s.residual_likelihood);
      byResidualCell.set(rk, [...(byResidualCell.get(rk) ?? []), r]);
      if (hasMoved(r.s)) {
        const ik = cellKey(r.s.inherent_impact, r.s.inherent_likelihood);
        byInherentCell.set(ik, [...(byInherentCell.get(ik) ?? []), r]);
      }
    }
    return { byResidualCell, byInherentCell };
  }, [ranked]);

  const toggleExpand = (key: string) =>
    setExpandedCells((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <Tooltip.Provider delayDuration={150} skipDelayDuration={400}>
      <section className="rounded-xl border border-ink-200 bg-white p-5 shadow-card">
        <header className="mb-4 flex items-center justify-between gap-3">
          <div className="text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
            Residual risk matrix
          </div>
          <button
            type="button"
            aria-pressed={showInherent}
            onClick={() => setShowInherent((v) => !v)}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium transition motion-reduce:transition-none",
              "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-900",
              showInherent
                ? "border-ink-800 bg-ink-800 text-white"
                : "border-ink-200 text-ink-600 hover:border-ink-400",
            )}
          >
            {showInherent ? <EyeOff className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
            Inherent → residual
          </button>
        </header>

        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(340px,380px)_minmax(0,1fr)]">
          <RiskGrid
            ranked={ranked}
            byResidualCell={byResidualCell}
            byInherentCell={byInherentCell}
            activeCode={activeCode}
            showInherent={showInherent}
            expandedCells={expandedCells}
            onActive={setActiveCode}
            onToggleExpand={toggleExpand}
            onSelect={onSelect}
          />
          <ScenarioRegister
            ranked={ranked}
            activeCode={activeCode}
            showInherent={showInherent}
            onActive={setActiveCode}
            onSelect={onSelect}
          />
        </div>

        <footer className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-ink-100 pt-3 text-[11px] text-ink-600">
          {BAND_ORDER.map((b) => (
            <span key={b} className="flex items-center gap-1.5">
              <span className={clsx("h-2.5 w-2.5 rounded-sm ring-1 ring-inset", bandTint(b))} aria-hidden />
              {bandLabel(b)}
            </span>
          ))}
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-ink-900 px-1 text-[9px] font-bold text-white"
            >
              1
            </span>
            register number
          </span>
          {showInherent && (
            <span className="flex items-center gap-1.5">
              <span aria-hidden className="h-4 w-4 rounded-full border border-dashed border-ink-500 bg-white/80" />
              inherent position
            </span>
          )}
        </footer>
      </section>
    </Tooltip.Provider>
  );
}

function ScenarioRegister({
  ranked,
  activeCode,
  showInherent,
  onActive,
  onSelect,
}: {
  ranked: Ranked[];
  activeCode: string | null;
  showInherent: boolean;
  onActive: (code: string | null) => void;
  onSelect: (code: string) => void;
}) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 flex items-baseline justify-between text-[11px] uppercase tracking-wider text-ink-500 font-semibold">
        Scenarios by residual risk
        <span className="normal-case tracking-normal font-normal tabular-nums">{ranked.length}</span>
      </div>
      {ranked.length === 0 ? (
        <div className="rounded-md border border-dashed border-ink-200 px-3 py-6 text-center text-xs text-ink-500">
          No scored scenarios yet.
        </div>
      ) : (
        <ol className="scrollable max-h-[440px] divide-y divide-ink-100 overflow-y-auto rounded-md border border-ink-200">
          {ranked.map((item) => {
            const { s, rank } = item;
            const active = activeCode === s.code;
            const moved = showInherent && hasMoved(s);
            return (
              <li key={s.code}>
                <button
                  type="button"
                  aria-label={scenarioAriaLabel(item)}
                  onClick={() => onSelect(s.code)}
                  onMouseEnter={() => onActive(s.code)}
                  onMouseLeave={() => onActive(null)}
                  onFocus={() => onActive(s.code)}
                  onBlur={() => onActive(null)}
                  className={clsx(
                    "flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors motion-reduce:transition-none",
                    "hover:bg-ink-50 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ink-900",
                    active && "bg-ink-100",
                  )}
                >
                  <span
                    className={clsx(
                      "inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full px-1 text-[10px] font-bold tabular-nums text-white",
                      active ? "bg-ink-950" : "bg-ink-900",
                    )}
                  >
                    {rank}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs font-semibold text-ink-900" title={s.name}>
                      {s.name}
                    </span>
                    <span className="flex items-center gap-1.5 text-[10px] text-ink-500">
                      <span className="truncate font-mono uppercase">{s.code}</span>
                      <span className="shrink-0 whitespace-nowrap tabular-nums text-ink-600">
                        {moved && (
                          <>
                            <span className="text-ink-400 line-through decoration-ink-300">
                              I{s.inherent_impact} L{s.inherent_likelihood}
                            </span>
                            <ArrowRight className="mx-0.5 inline h-3 w-3 text-ink-400" aria-hidden />
                          </>
                        )}
                        I{s.residual_impact} L{s.residual_likelihood}
                      </span>
                    </span>
                  </span>
                  <span
                    className={clsx(
                      "shrink-0 rounded-md px-2 py-1 text-[11px] font-semibold text-white",
                      bandColor(s.band),
                    )}
                  >
                    {bandLabel(s.band)}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
