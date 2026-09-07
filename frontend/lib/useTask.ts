"use client";

import { QueryObserver, useMutation, useQuery, type QueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { TaskStatus } from "./types";

export const isLiveTask = (t: TaskStatus | undefined) =>
  !!t && (t.status === "pending" || t.status === "running");

// One react-query entry per task id. The hook (renders the indicator) and
// waitForTask (lets a mutation await completion) share it, so a page that
// does both still issues one request per interval.
export function taskQueryOptions(taskId: string) {
  return {
    queryKey: ["task", taskId] as const,
    queryFn: ({ signal }: { signal?: AbortSignal }) => api.taskStatus(taskId, signal),
    refetchInterval: (q: { state: { data?: TaskStatus } }) => (isLiveTask(q.state.data) ? 800 : false),
    staleTime: 0,
  };
}

export function useTask(taskId: string | null | undefined) {
  const { data: task } = useQuery({
    ...taskQueryOptions(taskId ?? ""),
    enabled: !!taskId,
  });
  const cancel = useMutation({ mutationFn: (tid: string) => api.cancelTask(tid) });
  return { task: taskId ? task : undefined, isLive: isLiveTask(task), cancel };
}

// Resolves when the task lands, rejects with the backend's detail when it
// fails (a cancelled task rejects with "cancelled by user"). Subscribes to
// the same query the hook uses instead of running a second polling loop.
export function waitForTask(qc: QueryClient, taskId: string): Promise<TaskStatus> {
  return new Promise((resolve, reject) => {
    const observer = new QueryObserver(qc, taskQueryOptions(taskId));
    let unsub: () => void = () => {};
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      unsub();
      fn();
    };
    const check = (r: { data?: TaskStatus; error: unknown }) => {
      const s = r.data;
      if (!s) {
        if (r.error) finish(() => reject(r.error));
        return;
      }
      if (s.status === "done") finish(() => resolve(s));
      else if (s.status === "error") finish(() => reject(new Error(s.detail || s.error || "Task failed")));
    };
    unsub = observer.subscribe(check);
    check(observer.getCurrentResult());
  });
}
