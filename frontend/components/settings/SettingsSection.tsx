import clsx from "clsx";

// Layout primitives for the Settings page. Every row in every section uses
// the same three-column template (label | control | status), which is what
// keeps values aligned across sections when they are read at a glance.

const ROW_GRID = "grid grid-cols-[220px_minmax(0,1fr)_120px] items-start gap-x-6";

export function SettingsSection({
  title,
  description,
  status,
  footer,
  children,
}: {
  title: string;
  description?: string;
  status?: React.ReactNode;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-ink-200 bg-white">
      <div className="flex items-start justify-between gap-4 px-5 py-3 border-b border-ink-100">
        <div className="min-w-0">
          <h3 className="text-xs uppercase tracking-wide text-ink-500 font-semibold">{title}</h3>
          {description && <p className="text-xs text-ink-600 mt-0.5">{description}</p>}
        </div>
        {status && <div className="shrink-0 text-xs text-ink-600">{status}</div>}
      </div>
      <div className="px-5 divide-y divide-ink-100">{children}</div>
      {footer && <div className="px-5 py-3 border-t border-ink-100">{footer}</div>}
    </section>
  );
}

export function SettingsRow({
  label,
  hint,
  status,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  status?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className={clsx(ROW_GRID, "py-2.5")}>
      <div className="min-w-0 pt-1">
        <div className="text-xs font-medium text-ink-800">{label}</div>
        {hint && <div className="text-[10px] uppercase tracking-wide text-ink-500 mt-0.5">{hint}</div>}
      </div>
      <div className="min-w-0">{children}</div>
      <div className="min-w-0 pt-1 flex justify-end">{status}</div>
    </div>
  );
}

export function Chip({ tone, children }: { tone: "muted" | "amber" | "emerald"; children: React.ReactNode }) {
  return (
    <span
      className={clsx(
        "inline-block rounded text-[9px] font-semibold uppercase tracking-wide px-1 py-px whitespace-nowrap",
        tone === "muted" && "bg-ink-100 text-ink-600",
        tone === "amber" && "bg-amber-100 text-amber-800",
        tone === "emerald" && "bg-emerald-100 text-emerald-800",
      )}
    >
      {children}
    </span>
  );
}

export const INPUT_CLASS = "rounded border border-ink-200 px-2 py-1 text-xs bg-white disabled:opacity-40";
