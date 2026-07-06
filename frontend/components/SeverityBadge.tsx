"use client";

import clsx from "clsx";
import { WeaknessRead } from "@/lib/types";
import { SEVERITY_STYLES } from "@/lib/utils";

export function SeverityBadge({
  severity,
  className,
}: {
  severity: WeaknessRead["severity"];
  className?: string;
}) {
  const sev = SEVERITY_STYLES[severity];
  return (
    <span className={clsx("inline-flex items-center gap-1.5", className)}>
      <span className={clsx("h-1.5 w-1.5 rounded-full shrink-0", sev.dot)} aria-hidden />
      <span className={clsx("text-[10px] uppercase tracking-wider font-bold", sev.text)}>
        {severity}
      </span>
    </span>
  );
}
