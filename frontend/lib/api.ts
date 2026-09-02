import { Assessment, ScenarioRead, DescriptionRead, DocumentRead, ChunkRead, AggregateScoreRead, ScenarioScoreRead, ReportOut, ModelProfile, WeaknessRead, MetaIssueRead, StandardsProfile, WorkflowConflictDetail } from "./types";

// Every non-2xx response. `detail` is the parsed JSON `detail` when the body
// was JSON (FastAPI's shape), otherwise the raw text.
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function raise(r: Response): Promise<never> {
  const text = await r.text();
  let detail: unknown = text;
  try {
    detail = JSON.parse(text)?.detail ?? text;
  } catch {
    /* not JSON */
  }
  const message =
    typeof detail === "string"
      ? detail
      : detail && typeof detail === "object" && "message" in detail && typeof (detail as { message: unknown }).message === "string"
        ? (detail as { message: string }).message
        : `HTTP ${r.status}: ${text}`;
  throw new ApiError(r.status, detail, message);
}

// The workflow guards answer 409 with a structured body (see
// backend app.workflow); null for any other error.
export function conflictDetail(e: unknown): WorkflowConflictDetail | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const d = e.detail as Partial<WorkflowConflictDetail> | null;
  if (!d || typeof d !== "object" || !Array.isArray(d.missing) || typeof d.message !== "string") return null;
  return d as WorkflowConflictDetail;
}

// Human-readable line for any thrown error (workflow 409s come through as
// their reason; everything else as its message).
export function describeError(e: unknown): string {
  const c = conflictDetail(e);
  if (c) return c.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

async function http<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const r = await fetch(input, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!r.ok) await raise(r);
  if (r.status === 204) return undefined as unknown as T;
  return (await r.json()) as T;
}

export const api = {
  // assessments
  listAssessments: () => http<Assessment[]>(`/api/assessments`),
  createAssessment: (vendor_name: string) =>
    http<Assessment>(`/api/assessments`, { method: "POST", body: JSON.stringify({ vendor_name }) }),
  getAssessment: (id: number) => http<Assessment>(`/api/assessments/${id}`),
  deleteAssessment: (id: number) => http<void>(`/api/assessments/${id}`, { method: "DELETE" }),

  // assessment-level inputs: analysis date + assessor standards
  patchSettings: (
    id: number,
    patch: { as_of_date?: string; clear_as_of_date?: boolean; standards_profile?: StandardsProfile },
  ) => http<Assessment>(`/api/assessments/${id}/settings`, { method: "PATCH", body: JSON.stringify(patch) }),

  // description / scoping
  setDescription: (id: number, text: string) =>
    http<DescriptionRead>(`/api/assessments/${id}/description`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  getDescription: (id: number) =>
    http<DescriptionRead | null>(`/api/assessments/${id}/description`),
  scopingTurn: (id: number, answer?: string) =>
    http<{ task_id: string }>(`/api/assessments/${id}/scoping/turn`, {
      method: "POST",
      body: JSON.stringify(answer ? { answer } : {}),
    }),
  forceContinue: (id: number) =>
    http<DescriptionRead>(`/api/assessments/${id}/scoping/force-continue`, { method: "POST" }),

  // documents
  listDocuments: (id: number) => http<DocumentRead[]>(`/api/assessments/${id}/documents`),
  uploadDocument: async (id: number, kind: string, file: File): Promise<DocumentRead> => {
    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("file", file);
    const r = await fetch(`/api/assessments/${id}/documents`, { method: "POST", body: fd });
    if (!r.ok) await raise(r);
    return r.json();
  },
  deleteDocument: (id: number) => http<void>(`/api/documents/${id}`, { method: "DELETE" }),
  // Retry path for a failed / interrupted per-document extraction.
  extractWeaknesses: (docId: number) =>
    http<{ task_id: string; status: string }>(`/api/documents/${docId}/extract-weaknesses`, { method: "POST" }),
  documentChunks: (id: number, q?: string) =>
    http<ChunkRead[]>(`/api/documents/${id}/chunks${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  getChunk: (id: number) => http<ChunkRead>(`/api/chunks/${id}`),
  documentSignedUrl: (id: number) =>
    http<{ url: string; filename: string }>(`/api/documents/${id}/url`),

  // scenarios
  listScenarios: (id: number) => http<ScenarioRead[]>(`/api/assessments/${id}/scenarios`),
  generateScenarios: (id: number) =>
    http<{ task_id: string; status: string; progress: number; detail: string }>(
      `/api/assessments/${id}/scenarios/generate`,
      { method: "POST" },
    ),
  patchScenario: (id: number, patch: Partial<{ inherent_impact: number; inherent_likelihood: number; name: string; description: string }>) =>
    http<ScenarioRead>(`/api/scenarios/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteScenario: (id: number) =>
    http<void>(`/api/scenarios/${id}`, { method: "DELETE" }),
  patchControlAssessment: (caId: number, patch: Partial<{ coverage: string; effectiveness: string; rationale: string; is_locked_by_user: boolean }>) =>
    http<ScenarioRead>(`/api/control-assessments/${caId}`, { method: "PATCH", body: JSON.stringify(patch) }),
  upsertControlAssessment: (ecId: number, patch: { coverage?: string; effectiveness?: string; rationale?: string }) =>
    http<ScenarioRead>(`/api/expected-controls/${ecId}/assess`, { method: "POST", body: JSON.stringify(patch) }),
  createExpectedControl: (scenarioId: number, payload: { code: string; name: string; description?: string; weight?: number; rationale?: string }) =>
    http<ScenarioRead>(`/api/scenarios/${scenarioId}/expected-controls`, { method: "POST", body: JSON.stringify(payload) }),
  deleteExpectedControl: (id: number) =>
    http<void>(`/api/expected-controls/${id}`, { method: "DELETE" }),

  // gap analysis / weaknesses / scoring
  runGapAnalysis: (id: number, onlyFailed = false) =>
    http<{ task_id: string }>(
      `/api/assessments/${id}/gap-analysis/run${onlyFailed ? "?only_failed=true" : ""}`,
      { method: "POST" },
    ),
  assessControlAI: (ecId: number) =>
    http<{ task_id: string }>(`/api/expected-controls/${ecId}/assess-ai`, { method: "POST" }),
  synthesizeWeaknesses: (id: number) =>
    http<{ task_id: string }>(`/api/assessments/${id}/weaknesses/synthesize`, { method: "POST" }),
  recalculate: (id: number) =>
    http<{ scenarios: ScenarioScoreRead[]; aggregate: AggregateScoreRead }>(
      `/api/assessments/${id}/recalculate`,
      { method: "POST" }
    ),
  runNarratives: (id: number) =>
    http<{ task_id: string }>(`/api/assessments/${id}/narratives/run`, { method: "POST" }),
  runExecutiveSummary: (id: number) =>
    http<{ task_id: string }>(`/api/assessments/${id}/executive-summary/run`, { method: "POST" }),
  listWeaknesses: (id: number, includeAll = false) =>
    http<WeaknessRead[]>(`/api/assessments/${id}/weaknesses${includeAll ? "?include=all" : ""}`),
  report: (id: number) => http<ReportOut>(`/api/assessments/${id}/report`),

  // models
  listModels: () => http<ModelProfile[]>(`/api/models`),
  setModelOverrides: (id: number, overrides: Record<string, string>) =>
    http<Assessment>(`/api/assessments/${id}/model-overrides`, {
      method: "PATCH",
      body: JSON.stringify(overrides),
    }),

  // tasks
  taskStatus: (taskId: string) =>
    http<{ task_id: string; status: string; progress: number; detail: string }>(
      `/api/tasks/${taskId}`
    ),
};

export async function pollTask(
  taskId: string,
  onProgress?: (p: { status: string; progress: number; detail: string }) => void,
  intervalMs = 500,
): Promise<void> {
  for (;;) {
    const s = await api.taskStatus(taskId);
    onProgress?.(s);
    if (s.status === "done") return;
    if (s.status === "error") throw new Error(s.detail || "Task failed");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
