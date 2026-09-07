"use client";

import { Sparkles } from "lucide-react";
import clsx from "clsx";
import type { ActivitySource } from "@/lib/types";
import { KIND_LABEL, formatDuration, formatTokens, purposeLabel, useElapsed } from "@/lib/activity";
import { verbAt, verbsFor } from "@/lib/activityVerbs";

// The one indicator for every AI-driven step.
//
//   panel  — card: glyph + rotating verb, status line, honest bar, stepper,
//            optional hint. Pages with a single long job.
//   inline — same content without the card chrome. Step rows and editors.
//   chip   — tiny glyph + stage text. Document rows, nav badges.
//
// Everything shown is observed: the stage and stepper come from the job,
// the bar is drawn only when the current stage has a unit count, elapsed
// time is the server's, tokens are the streamed count. No percentages.

export type AiActivityVariant = "panel" | "inline" | "chip";

export function AiActivity({
  source,
  kind,
  variant = "panel",
  onCancel,
  cancelling,
  hint,
  startedAtMs,
  className,
}: {
  source?: ActivitySource | null;
  kind?: string;
  variant?: AiActivityVariant;
  onCancel?: () => void;
  cancelling?: boolean;
  hint?: string;
  // Client-only sources (a synchronous call with no task): when the job started.
  startedAtMs?: number;
  className?: string;
}) {
  const s = source ?? {};
  const elapsed = useElapsed(s.elapsed_s, startedAtMs);
  const stage = s.stage || "";
  const stages = s.stages ?? [];
  const stageIndex = s.stage_index ?? -1;
  const unitsTotal = s.units_total ?? 0;
  const unitsDone = Math.min(s.units_done ?? 0, unitsTotal || Infinity);
  const tokens = s.tokens_out ?? 0;
  const callsActive = s.calls_active ?? 0;
  const idle = s.idle_s != null ? Math.round(s.idle_s) : null;
  const { verb, index: verbIndex } = verbAt(verbsFor(kind, stage), elapsed);
  const label = (kind && KIND_LABEL[kind]) || "AI";
  const doing = purposeLabel(s.purpose);

  if (variant === "chip") {
    const text = stage
      ? unitsTotal > 0
        ? `${stage} · ${unitsDone}/${unitsTotal}`
        : stage
      : "working";
    return (
      <span
        className={clsx(
          "inline-flex items-center gap-1.5 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-amber-700",
          className,
        )}
        title={[doing, `${label} · ${formatDuration(elapsed)}`].filter(Boolean).join(" — ")}
        role="status"
      >
        <AiGlyph size={10} />
        <span className="truncate max-w-[16rem]">{text}</span>
      </span>
    );
  }

  const body = (
    <>
      <div className="flex items-center gap-2.5">
        <AiGlyph size={variant === "panel" ? 20 : 16} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2 min-w-0">
            <span
              key={verbIndex}
              aria-hidden
              className="animate-ai-fade motion-reduce:animate-none truncate text-[13px] font-medium text-ink-900"
              title={doing ?? undefined}
            >
              {verb}…
            </span>
          </div>
          <div role="status" aria-live="polite" className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[11px] text-ink-500 tabular-nums">
            <span className="text-ink-600">{label}</span>
            <Item title="Wall-clock time since the job started (server-side)">{formatDuration(elapsed)}</Item>
            {tokens > 0 && (
              <Item title="Output tokens streamed so far across every model call of this job (exact after each call; estimated while a stream is open)">
                ↓ {formatTokens(tokens)} tokens
              </Item>
            )}
            {unitsTotal > 0 && (
              <Item className="text-ink-700 font-medium">
                {unitsDone} / {unitsTotal} {s.unit_label || "units"}
              </Item>
            )}
            {callsActive > 0 && (
              <Item title="Model calls currently streaming">
                {callsActive} call{callsActive === 1 ? "" : "s"} in flight
              </Item>
            )}
            {idle != null && idle >= 60 && (
              <Item className="text-amber-700" title="No token, keepalive or progress from the job for this long">
                idle {formatDuration(idle)}
              </Item>
            )}
          </div>
        </div>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            title="Stop this job; the step becomes re-runnable"
            className="ml-auto shrink-0 self-start rounded border border-ink-300 px-2 py-0.5 text-[11px] font-medium text-ink-700 hover:bg-ink-50 disabled:opacity-40"
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </div>

      <ProgressTrack done={unitsDone} total={unitsTotal} className="mt-2.5" />

      {stages.length > 1 && (
        <Stepper stages={stages} current={stageIndex} className="mt-2" />
      )}

      {s.detail && variant === "panel" && (
        <div className="mt-2 truncate text-[11px] text-ink-500" title={s.detail}>
          {s.detail}
        </div>
      )}
      {hint && <div className="mt-2 text-[11px] text-ink-400">{hint}</div>}
    </>
  );

  if (variant === "inline") {
    return <div className={clsx("min-w-0", className)}>{body}</div>;
  }
  return (
    <div className={clsx("rounded-lg border border-ink-200 bg-white p-3.5 shadow-card", className)}>
      {body}
    </div>
  );
}

// Sparkle inside two counter-rotating rings. Decorative — the text beside
// it carries the meaning.
export function AiGlyph({ size = 18, className }: { size?: number; className?: string }) {
  const icon = Math.max(6, Math.round(size * 0.5));
  return (
    <span
      aria-hidden
      className={clsx("relative inline-flex shrink-0 items-center justify-center", className)}
      style={{ width: size, height: size }}
    >
      <span className="absolute inset-0 rounded-full border border-amber-400/80 border-t-transparent animate-ai-orbit motion-reduce:animate-none" />
      <span className="absolute inset-[18%] rounded-full border border-ink-300 border-b-transparent animate-ai-orbit-slow motion-reduce:animate-none" />
      <Sparkles
        className="text-amber-500 animate-ai-breathe motion-reduce:animate-none"
        style={{ width: icon, height: icon }}
        strokeWidth={2.25}
      />
    </span>
  );
}

// Determinate only when the stage has a real unit count; otherwise a
// shimmer that says "alive" without pretending to know how far along.
function ProgressTrack({ done, total, className }: { done: number; total: number; className?: string }) {
  const determinate = total > 0;
  const pct = determinate ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
  return (
    <div
      className={clsx("relative h-1.5 w-full overflow-hidden rounded-full bg-ink-100", className)}
      role={determinate ? "progressbar" : undefined}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? total : undefined}
      aria-valuenow={determinate ? done : undefined}
    >
      {determinate ? (
        <div
          className="h-full rounded-full bg-amber-400 transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      ) : (
        <div className="absolute inset-y-0 left-0 w-1/3 bg-gradient-to-r from-transparent via-amber-400 to-transparent animate-ai-shimmer motion-reduce:animate-none motion-reduce:w-full motion-reduce:via-amber-300" />
      )}
    </div>
  );
}

function Stepper({ stages, current, className }: { stages: string[]; current: number; className?: string }) {
  return (
    <ol className={clsx("flex flex-wrap items-center gap-x-3 gap-y-1", className)} aria-label="Stages">
      {stages.map((name, i) => {
        const state = i < current ? "done" : i === current ? "current" : "pending";
        return (
          <li key={name} className="flex items-center gap-1.5 text-[11px]" aria-current={state === "current" ? "step" : undefined}>
            {state === "done" && (
              <span className="flex h-3 w-3 items-center justify-center rounded-full bg-emerald-500 text-[8px] font-bold leading-none text-white">✓</span>
            )}
            {state === "current" && (
              <span className="flex h-3 w-3 items-center justify-center">
                <span className="h-2 w-2 rounded-full bg-amber-400 animate-ai-breathe motion-reduce:animate-none" />
              </span>
            )}
            {state === "pending" && <span className="h-3 w-3 rounded-full border border-ink-300" />}
            <span className={clsx(
              state === "done" && "text-ink-500",
              state === "current" && "font-medium text-ink-900",
              state === "pending" && "text-ink-400",
            )}>
              {name}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// A status-line item carries its own leading separator so a wrap never
// strands the dot at the end of a line.
function Item({ children, title, className }: { children: React.ReactNode; title?: string; className?: string }) {
  return (
    <span className="inline-flex items-center gap-x-1.5 whitespace-nowrap">
      <span aria-hidden className="text-ink-300">·</span>
      <span className={className} title={title}>{children}</span>
    </span>
  );
}
