"use client";

import Link from "next/link";
import { useWorkflow, staleLine } from "@/lib/useWorkflow";

// Shown at the top of the score and report pages when the scored state they
// render is not the current pipeline output: the narratives step has not run,
// is running, or an upstream input changed after it ran (stale). The scores
// below stay visible — they are deterministic — but the banner says what
// must happen before they can be relied on.
export function PrerequisitesBanner({ assessmentId }: { assessmentId: number }) {
  const { assessment, step } = useWorkflow(assessmentId);
  if (!assessment) return null;
  const score = step("score");
  const info = score.info;
  const stale = staleLine(score.stale);

  if (info?.state === "done" && !stale) return null;

  let headline: string;
  let detail: string | null = null;
  if (info?.state === "running") {
    headline = "Narratives are being written — this page refreshes when they land.";
  } else if (info?.state === "done" && stale) {
    headline = "These results are stale.";
    detail = `${stale}. Re-run the affected steps before relying on this page.`;
  } else if (!score.canRun) {
    headline = "Earlier steps have not completed.";
    detail = score.blockedReason;
  } else {
    headline = "Narratives have not been written yet.";
    detail = "Run cross-correlation, gap analysis and narratives on the analysis page first.";
  }

  return (
    <div className="mb-5 max-w-5xl rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 flex items-start justify-between gap-4">
      <div>
        <div className="font-semibold">{headline}</div>
        {detail && <div className="mt-0.5 text-xs">{detail}</div>}
      </div>
      <Link
        href={`/assessments/${assessmentId}/analysis`}
        className="shrink-0 rounded border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
      >
        Go to analysis →
      </Link>
    </div>
  );
}
