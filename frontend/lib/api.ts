import { Assessment, ScenarioRead, DescriptionRead, DocumentRead, ChunkRead, AggregateScoreRead, ScenarioScoreRead, ReportOut, ModelProfile, WeaknessRead, MetaIssueRead } from "./types";

async function http<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const r = await fetch(input, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
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

  // description / scoping
  setDescription: (id: number, text: string) =>
    http<DescriptionRead>(`/api/assessments/${id}/description`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  getDescription: (id: number) =>
    http<DescriptionRead | null>(`/api/assessments/${id}/description`),
  scopingTurn: (id: number, answer?: string) =>
    http<DescriptionRead>(`/api/assessments/${id}/scoping/turn`, {
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
    if (!r.ok) throw new Error(`Upload failed: ${r.status} ${await r.text()}`);
    return r.json();
  },
  deleteDocument: (id: number) => http<void>(`/api/documents/${id}`, { method: "DELETE" }),
  documentChunks: (id: number, q?: string) =>
    http<ChunkRead[]>(`/api/documents/${id}/chunks${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  getChunk: (id: number) => http<ChunkRead>(`/api/chunks/${id}`),
  documentSignedUrl: (id: number) =>
    http<{ url: string; filename: string }>(`/api/documents/${id}/url`),

  // scenarios
  listScenarios: (id: number) => http<ScenarioRead[]>(`/api/assessments/${id}/scenarios`),
  generateScenarios: (id: number) =>
    http<{ task_id: string }>(`/api/assessments/${id}/scenarios/generate`, { method: "POST" }),
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
  runGapAnalysis: (id: number) =>
    http<{ task_id: string }>(`/api/assessments/${id}/gap-analysis/run`, { method: "POST" }),
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
  listWeaknesses: (id: number) => http<WeaknessRead[]>(`/api/assessments/${id}/weaknesses`),
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
