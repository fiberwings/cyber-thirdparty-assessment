"use client";

import { useEffect, useState } from "react";

// Mount only while a task is in flight — the elapsed timer starts at mount.
export function TaskProgress({ detail, label = "Processing" }: { detail?: string; label?: string }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const start = Date.now();
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="flex items-center gap-2 rounded-lg border border-ink-200 bg-white p-3 text-xs text-ink-700">
      <span className="h-4 w-4 shrink-0 rounded-full border-2 border-amber-400 border-t-transparent animate-spin" />
      <span>
        {label}… {elapsed}s
      </span>
      {detail && <span className="text-ink-500">· {detail}</span>}
    </div>
  );
}
