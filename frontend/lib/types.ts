// Hand-mirrored from backend/app/schemas/api.py — kept narrow on purpose.

export type Coverage = "none" | "partial" | "full";
export type Effectiveness = "weak" | "adequate" | "strong" | "unknown";
export type Band = "Low" | "Moderate" | "High" | "VeryHigh";

export type PhaseState = "pending" | "running" | "done" | "error";

// A completed step whose inputs changed afterwards. The result stays visible
// but blocks every downstream step until the stamped step is re-run.
export interface StaleInfo {
  at: string;
  reasons: string[];
  // Whether "re-run failed only" / per-control re-assess can still address it
  // (true only when controls were added).
  resume_ok: boolean;
}

export type BlockCode =
  | "missing_description"
  | "prerequisite_pending"
  | "prerequisite_running"
  | "prerequisite_error"
  | "prerequisite_stale"
  | "no_documents"
  | "extraction_incomplete"
  | "extraction_failed"
  | "run_in_flight"
  | "resume_requires_full_run";

export interface BlockReason {
  code: BlockCode;
  step: string | null;
  message: string;
  task_id?: string | null;
  kind?: string | null;
  document_ids?: number[];
}

// Body of every 409 raised by the backend workflow guards.
export interface WorkflowConflictDetail {
  code: BlockCode;
  step: string;
  missing: BlockReason[];
  message: string;
}

// Structured, observed progress of a running job (backend TaskProgressFields).
// `units_total === 0` means the current stage has no knowable unit count:
// the UI shows an indeterminate state and never invents a percentage.
export interface TaskProgressFields {
  stage?: string;
  stage_index?: number;
  stages?: string[];
  units_done?: number;
  units_total?: number;
  unit_label?: string;
  // Router purpose of the most recent LLM call (e.g. "weakness_confirmation").
  purpose?: string;
  calls_active?: number;
  calls_done?: number;
  // Cumulative streamed output tokens (estimated while a stream is open,
  // exact once the provider's usage chunk arrives).
  tokens_out?: number;
  tokens_reasoning?: number;
  first_token_ms?: number | null;
  // Server-side wall clock since the task started; the client ticks from it.
  elapsed_s?: number | null;
}

// What the AI activity indicator reads. A task poll payload, a running
// phase, or a client-only stub for synchronous calls all satisfy it.
export type ActivitySource = TaskProgressFields & {
  detail?: string | null;
  idle_s?: number | null;
};

export interface PhaseInfo extends TaskProgressFields {
  state: PhaseState;
  started_at: string | null;
  completed_at: string | null;
  task_id: string | null;
  error: string | null;
  detail: string | null;
  progress: number | null;
  // Liveness while running: last activity stamp (streamed token, keepalive
  // or progress step) and seconds since. Waits are based on these, not on
  // how long a step "should" take.
  last_activity_at?: string | null;
  idle_s?: number | null;
  // done-with-partial-failures (gap analysis): resumable per control
  warning?: string | null;
  failed_targets?: string[];
  // Workflow: set on a done step whose inputs changed afterwards.
  stale?: StaleInfo | null;
  // Workflow: whether the step may be (re)started now, and why not.
  ready?: boolean;
  blocked_by?: BlockReason[];
}

// GET /api/tasks/{id} (also returned when a job is submitted / re-attached).
export interface TaskStatus extends TaskProgressFields {
  task_id: string;
  status: string; // pending|running|done|error
  progress: number;
  detail: string;
  kind?: string;
  error?: string;
  started_at?: string | null;
  last_activity_at?: string | null;
  idle_s?: number | null;
}

export interface VulnSla {
  critical_days?: number | null;
  high_days?: number | null;
  medium_days?: number | null;
}

// Assessor standards profile (client-side requirements) — every field optional.
export interface StandardsProfile {
  required_attestations: string[];
  attestation_max_age_months?: number | null;
  pentest_max_age_months?: number | null;
  policy_review_months?: number | null;
  retention_years?: number | null;
  allowed_residency: string[];
  mfa_policy?: string | null;
  vuln_remediation_sla?: VulnSla | null;
  other_requirements: string[];
}

export const EMPTY_STANDARDS: StandardsProfile = {
  required_attestations: [],
  allowed_residency: [],
  other_requirements: [],
};

export const PHASE_KEYS = ["scoping", "scenarios", "evidence", "analysis", "score"] as const;
export type PhaseKey = (typeof PHASE_KEYS)[number];
// Every key in AssessmentRead.phases, in workflow order.
export const WORKFLOW_KEYS = ["scoping", "scenarios", "evidence", "correlation", "analysis", "score"] as const;
export type WorkflowKey = (typeof WORKFLOW_KEYS)[number];

export const PHASE_LABELS: Record<PhaseKey, string> = {
  scoping: "Scoping",
  scenarios: "Inherent risk",
  evidence: "Evidence",
  analysis: "Gap analysis",
  score: "Residual score",
};

export interface Assessment {
  id: number;
  vendor_name: string;
  status: string;
  current_phase: string;
  force_continued: boolean;
  model_overrides: Record<string, string>;
  created_at: string;
  updated_at: string;
  // Effective analysis date (today when not pinned) + whether it was pinned.
  as_of_date: string;
  as_of_date_set: boolean;
  standards_profile: StandardsProfile;
  // "correlation" is a sixth, non-nav key: the cross-correlation step that
  // sits between evidence and gap analysis (surfaced on the analysis page).
  phases: Partial<Record<WorkflowKey, PhaseInfo>>;
}

export interface Turn { id: number; role: string; content: string; created_at: string; }
export interface DescriptionRead {
  text: string;
  is_sufficient: boolean;
  sufficiency_json: Record<string, number | string>;
  turns: Turn[];
}

export interface DocumentRead {
  id: number;
  kind: string;
  filename: string;
  mime: string;
  size_bytes: number;
  parsed_at: string | null;
  // Per-document weakness extraction (the evidence step is done once every
  // document reports "done").
  weakness_extracted_at?: string | null;
  weakness_task_id?: string | null;
  weakness_error?: string | null;
  extraction_state?: "pending" | "running" | "done" | "error";
  // Typed attestation profile (SOC / ISO / pen-test docs); every field
  // carries the quote it came from. Shape mirrors backend AttestationProfileOut.
  attestation_profile?: Record<string, any> | null;
  // Why the last profile extraction failed (null once one succeeds).
  attestation_profile_error?: string | null;
}

export interface ChunkRead {
  id: number;
  document_id: number;
  page: number | null;
  section_path: string;
  text: string;
}

export interface CitationRead {
  document_id: number;
  chunk_id: number;
  page: number | null;
  section_path: string;
  quote: string;
  polarity: string;
}

export interface UnresolvedCitationRead {
  document_id: number | null;
  page: number | null;
  section_path: string;
  quote: string;
}

export interface ControlAssessmentRead {
  id: number;
  coverage: Coverage;
  effectiveness: Effectiveness;
  rationale: string;
  is_locked_by_user: boolean;
  citations: CitationRead[];
  unresolved_citations: UnresolvedCitationRead[];
  // Set when the last AI run for this control failed (verdict stale/absent).
  last_error?: string | null;
  last_run_at?: string | null;
}

export interface ExpectedControlRead {
  id: number;
  code: string;
  name: string;
  description: string;
  weight: number;
  rationale: string;
  assessment: ControlAssessmentRead | null;
}

export interface ScenarioRead {
  id: number;
  code: string;
  name: string;
  description: string;
  source: string;
  inherent_impact: number;
  inherent_likelihood: number;
  residual_impact: number;
  residual_likelihood: number;
  score_band: Band;
  rationale: string;
  user_edited: boolean;
  origin_weakness_ids: number[];
  expected_controls: ExpectedControlRead[];
}

export interface ScenarioScoreRead {
  code: string;
  name: string;
  band: Band;
  residual_impact: number;
  residual_likelihood: number;
  inherent_impact: number;
  inherent_likelihood: number;
  coverage_index: number;
  likelihood_reduction: number;
  uplift: number;
  distinct_high_critical: number;
  auditor_tested_high_critical: number;
  confidence: "high" | "medium" | "low";
  state_downgrades: string[];
  rationale: string;
}

export interface AggregateScoreRead {
  confidence?: "high" | "medium" | "low";
  band: Band;
  rank: number;
  weighted_mean_rank: number;
  top2_mean_rank: number;
}

export interface WeaknessRead {
  id: number;
  severity: "low" | "medium" | "high" | "critical";
  description: string;
  quote: string;
  mapped_control_codes: string[];
  source_chunk_id: number | null;
  source_document_id: number | null;
  unmatched: boolean;
  kind_signal: string;
  user_edited: boolean;
  origin: "document" | "gap_analysis" | string;
  evidence_refs: EvidenceRef[];
  origin_refs: string[];
  // Review status: only "confirmed" rows are reported and scored.
  status?: "candidate" | "confirmed" | "evidence_note" | "dropped" | "merged";
  review?: {
    decision?: string; confidence?: string; reason?: string; at?: string; stage?: string;
    unreviewed?: boolean; merged_into?: number; merge_reason?: string; members?: number[];
  } | null;
}

export interface EvidenceRef {
  document_id: number;
  chunk_id: number | null;
  page: number | null;
  section_path: string;
  quote: string;
}

export interface MetaIssueRead {
  id: number;
  kind: string;
  target_ref: string;
  weight: number;
  rationale: string;
  scenario_code: string | null;
}

export interface KeyRiskRead {
  title: string;
  why_it_matters: string;
  scenario_codes: string[];
  weakness_ids: number[];
  evidence_basis: string;
}

export interface RecommendedActionRead {
  action: string;
  priority: "immediate" | "near_term" | "monitor";
  related_scenario_codes: string[];
}

export interface ExecutiveSummaryRead {
  generated_at: string;
  model_id: string;
  stale: boolean;
  verdict: string;
  key_risks: KeyRiskRead[];
  limitations: string[];
  recommended_actions: RecommendedActionRead[];
}

export interface ReportOut {
  assessment: Assessment;
  description: DescriptionRead | null;
  documents: DocumentRead[];
  scenarios: ScenarioScoreRead[];
  weaknesses: WeaknessRead[];
  meta_issues: MetaIssueRead[];
  aggregate: AggregateScoreRead;
  executive_summary: ExecutiveSummaryRead | null;
}

export interface ModelProfile {
  name: string;
  default_model: string;
  alternatives: string[];
}

export const LEVEL_NAMES = ["Low", "Moderate", "High", "VeryHigh"] as const;
export const LEVEL_LABELS: Record<number, string> = { 1: "Low", 2: "Moderate", 3: "High", 4: "Very High" };
