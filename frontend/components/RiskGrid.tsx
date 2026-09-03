"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { ArrowRight, ArrowUp } from "lucide-react";
import clsx from "clsx";
import { LEVEL_LABELS, ScenarioScoreRead } from "@/lib/types";
import {
  bandAbbr,
  bandColor,
  bandLabel,
  bandTextColor,
  bandTint,
  driverLine,
} from "@/lib/utils";
import { IMPACT_ROWS, LEVELS, bandFor, cellCenter, cellKey } from "@/lib/riskMatrix";

export type Ranked = { s: ScenarioScoreRead; rank: number };

export function hasMoved(s: ScenarioScoreRead): boolean {
  return s.inherent_impact !== s.residual_impact || s.inherent_likelihood !== s.residual_likelihood;
}

export function scenarioAriaLabel({ s, rank }: Ranked): string {
  return `#${rank} ${s.code} — ${s.name}. Residual ${bandLabel(s.band)} (impact ${s.residual_impact}, likelihood ${s.residual_likelihood})`;
}

// Cells with more markers than this collapse to 3 + "+k" until expanded.
const MAX_VISIBLE = 4;

export function RiskGrid({
  ranked,
  byResidualCell,
  byInherentCell,
  activeCode,
  showInherent,
  expandedCells,
  onActive,
  onToggleExpand,
  onSelect,
}: {
  ranked: Ranked[];
  byResidualCell: Map<string, Ranked[]>;
  byInherentCell: Map<string, Ranked[]>;
  activeCode: string | null;
  showInherent: boolean;
  expandedCells: Set<string>;
  onActive: (code: string | null) => void;
  onToggleExpand: (key: string) => void;
  onSelect: (code: string) => void;
}) {
  const movers = ranked.filter((r) => hasMoved(r.s));
  const arrows = showInherent
    ? movers
    : movers.filter((r) => r.s.code === activeCode);

  return (
    <div className="grid grid-cols-[auto_auto_minmax(0,1fr)] gap-x-2 content-start self-start">
      {/* Impact axis title */}
      <div className="flex items-center justify-center pr-1">
        <span className="flex items-center gap-1 text-[10px] uppercase tracking-wider font-semibold text-ink-500 whitespace-nowrap [writing-mode:vertical-rl] rotate-180">
          Impact
          <ArrowUp className="h-3 w-3 rotate-90" aria-hidden />
        </span>
      </div>

      {/* Impact tick labels */}
      <div className="grid grid-rows-4 gap-1.5 pr-2 text-right">
        {IMPACT_ROWS.map((i) => (
          <TickLabel key={i} level={i} align="right" />
        ))}
      </div>

      {/* Cell block + overlay */}
      <div className="relative" role="group" aria-label="Residual risk matrix, impact by likelihood">
        <div className="grid grid-cols-4 grid-rows-4 gap-1.5">
          {IMPACT_ROWS.map((i) =>
            LEVELS.map((l) => (
              <MatrixCell
                key={cellKey(i, l)}
                impact={i}
                likelihood={l}
                residual={byResidualCell.get(cellKey(i, l)) ?? []}
                ghosts={showInherent ? byInherentCell.get(cellKey(i, l)) ?? [] : []}
                activeCode={activeCode}
                expanded={expandedCells.has(cellKey(i, l))}
                onActive={onActive}
                onToggleExpand={onToggleExpand}
                onSelect={onSelect}
              />
            )),
          )}
        </div>
        <MovementOverlay arrows={arrows} activeCode={activeCode} />
      </div>

      {/* Likelihood tick labels */}
      <div />
      <div />
      <div className="mt-1.5 grid grid-cols-4 gap-1.5 text-center">
        {LEVELS.map((l) => (
          <TickLabel key={l} level={l} align="center" />
        ))}
      </div>

      {/* Likelihood axis title */}
      <div />
      <div />
      <div className="mt-1 flex items-center justify-center gap-1 text-[10px] uppercase tracking-wider font-semibold text-ink-500">
        Likelihood
        <ArrowRight className="h-3 w-3" aria-hidden />
      </div>
    </div>
  );
}

function TickLabel({ level, align }: { level: number; align: "right" | "center" }) {
  return (
    <div className={clsx("self-center leading-tight", align === "right" ? "text-right" : "text-center")}>
      <div className="text-[11px] font-semibold text-ink-700 tabular-nums">{level}</div>
      <div className="text-[9px] text-ink-500 whitespace-nowrap">{LEVEL_LABELS[level]}</div>
    </div>
  );
}

function MatrixCell({
  impact,
  likelihood,
  residual,
  ghosts,
  activeCode,
  expanded,
  onActive,
  onToggleExpand,
  onSelect,
}: {
  impact: number;
  likelihood: number;
  residual: Ranked[];
  ghosts: Ranked[];
  activeCode: string | null;
  expanded: boolean;
  onActive: (code: string | null) => void;
  onToggleExpand: (key: string) => void;
  onSelect: (code: string) => void;
}) {
  const band = bandFor(impact, likelihood);
  const key = cellKey(impact, likelihood);
  const overflow = residual.length > MAX_VISIBLE && !expanded;
  const visible = overflow ? residual.slice(0, MAX_VISIBLE - 1) : residual;
  const dense = expanded && residual.length > MAX_VISIBLE;

  return (
    <div
      className={clsx(
        "relative aspect-square rounded-md ring-1 ring-inset p-1 overflow-hidden",
        bandTint(band),
      )}
    >
      <span
        aria-hidden
        className={clsx(
          "absolute left-1 top-0.5 text-[9px] font-bold leading-none opacity-70 select-none",
          bandTextColor(band),
        )}
      >
        {bandAbbr(band)}
      </span>

      <div className="relative z-20 flex h-full flex-wrap content-center items-center justify-center gap-1">
        {visible.map((r) => (
          <ScenarioChip
            key={r.s.code}
            item={r}
            active={activeCode === r.s.code}
            dimmed={activeCode !== null && activeCode !== r.s.code}
            dense={dense}
            onActive={onActive}
            onSelect={onSelect}
          />
        ))}
        {(overflow || dense) && (
          <button
            type="button"
            onClick={() => onToggleExpand(key)}
            aria-label={
              overflow
                ? `Show ${residual.length - visible.length} more scenarios in this cell`
                : "Collapse this cell"
            }
            className={clsx(
              chipBase(dense),
              "bg-white text-ink-800 ring-1 ring-inset ring-ink-400 hover:bg-ink-50",
            )}
          >
            {overflow ? `+${residual.length - visible.length}` : "–"}
          </button>
        )}
        {ghosts.map((r) => (
          <GhostChip
            key={`ghost-${r.s.code}`}
            item={r}
            active={activeCode === r.s.code}
            dimmed={activeCode !== null && activeCode !== r.s.code}
            dense={dense}
          />
        ))}
      </div>
    </div>
  );
}

function chipBase(dense: boolean): string {
  return clsx(
    "inline-flex items-center justify-center rounded-full px-1 font-bold tabular-nums leading-none",
    "transition-[opacity,transform] motion-reduce:transition-none",
    "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-900",
    dense ? "h-4 min-w-4 text-[9px]" : "h-5 min-w-5 text-[10px]",
  );
}

function ScenarioChip({
  item,
  active,
  dimmed,
  dense,
  onActive,
  onSelect,
}: {
  item: Ranked;
  active: boolean;
  dimmed: boolean;
  dense: boolean;
  onActive: (code: string | null) => void;
  onSelect: (code: string) => void;
}) {
  const { s, rank } = item;
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button
          type="button"
          aria-label={scenarioAriaLabel(item)}
          onMouseEnter={() => onActive(s.code)}
          onMouseLeave={() => onActive(null)}
          onFocus={() => onActive(s.code)}
          onBlur={() => onActive(null)}
          onClick={() => onSelect(s.code)}
          className={clsx(
            chipBase(dense),
            "text-white shadow-sm",
            active
              ? "bg-ink-950 outline outline-2 outline-offset-2 outline-ink-900 scale-110"
              : "bg-ink-900",
            dimmed && "opacity-40",
          )}
        >
          {rank}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side="top"
          sideOffset={6}
          collisionPadding={8}
          className="z-50 max-w-xs rounded-lg border border-ink-200 bg-white p-3 text-xs text-ink-700 shadow-lg"
        >
          <ScenarioTooltipContent item={item} />
          <Tooltip.Arrow className="fill-white" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

function GhostChip({
  item,
  active,
  dimmed,
  dense,
}: {
  item: Ranked;
  active: boolean;
  dimmed: boolean;
  dense: boolean;
}) {
  return (
    <span
      aria-hidden
      className={clsx(
        chipBase(dense),
        "border border-dashed bg-white/80 font-semibold",
        active ? "border-ink-900 text-ink-900 bg-white" : "border-ink-500 text-ink-600",
        dimmed && "opacity-30",
      )}
    >
      {item.rank}
    </span>
  );
}

// Percent units on a 100×100 viewBox. PAD keeps arrow ends clear of the
// markers; HEAD is the arrowhead length.
const PAD = 7;
const HEAD = 3.5;

function MovementOverlay({ arrows, activeCode }: { arrows: Ranked[]; activeCode: string | null }) {
  if (arrows.length === 0) return null;
  return (
    <svg
      aria-hidden
      className="pointer-events-none absolute inset-0 z-10 h-full w-full"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
    >
      {arrows.map(({ s }) => {
        const from = cellCenter(s.inherent_impact, s.inherent_likelihood);
        const to = cellCenter(s.residual_impact, s.residual_likelihood);
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        const len = Math.hypot(dx, dy);
        if (len === 0) return null;
        const u = { x: dx / len, y: dy / len };
        const n = { x: -u.y, y: u.x };
        const p1 = { x: from.x + u.x * PAD, y: from.y + u.y * PAD };
        const tip = { x: to.x - u.x * PAD, y: to.y - u.y * PAD };
        const base = { x: tip.x - u.x * HEAD, y: tip.y - u.y * HEAD };
        const head = [
          `${tip.x},${tip.y}`,
          `${base.x + n.x * 2},${base.y + n.y * 2}`,
          `${base.x - n.x * 2},${base.y - n.y * 2}`,
        ].join(" ");
        const active = activeCode === s.code;
        const dimmed = activeCode !== null && !active;
        return (
          <g
            key={s.code}
            className={clsx(
              "transition-opacity motion-reduce:transition-none",
              active ? "stroke-ink-900 fill-ink-900" : "stroke-ink-600 fill-ink-600",
              dimmed && "opacity-25",
            )}
          >
            <line
              x1={p1.x}
              y1={p1.y}
              x2={base.x}
              y2={base.y}
              strokeWidth={active ? 2.5 : 1.75}
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
            <polygon points={head} stroke="none" />
          </g>
        );
      })}
    </svg>
  );
}

function levelPair(impact: number, likelihood: number): string {
  return `I${impact} ${LEVEL_LABELS[impact]} × L${likelihood} ${LEVEL_LABELS[likelihood]}`;
}

function ScenarioTooltipContent({ item }: { item: Ranked }) {
  const { s, rank } = item;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-ink-900 px-1 text-[10px] font-bold tabular-nums text-white">
          {rank}
        </span>
        <span className="min-w-0 flex-1 font-mono text-[10px] uppercase text-ink-500 break-all">
          {s.code}
        </span>
        <span className={clsx("shrink-0 rounded-md text-white text-[11px] font-semibold px-2 py-1", bandColor(s.band))}>
          {bandLabel(s.band)}
        </span>
      </div>
      <div className="font-semibold text-ink-900">{s.name}</div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 tabular-nums">
        <dt className="text-ink-500">Inherent</dt>
        <dd>{levelPair(s.inherent_impact, s.inherent_likelihood)}</dd>
        <dt className="text-ink-500">Residual</dt>
        <dd className="font-medium text-ink-900">{levelPair(s.residual_impact, s.residual_likelihood)}</dd>
      </dl>
      <div className="text-[11px] text-ink-500">Evidence confidence: {s.confidence}</div>
      <div className="text-[11px] text-ink-500">{driverLine(s)}</div>
    </div>
  );
}
