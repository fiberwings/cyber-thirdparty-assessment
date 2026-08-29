// Hand-mirrored from backend/app/schemas/api.py — kept narrow on purpose.

export type Coverage = "none" | "partial" | "full";
export type Effectiveness = "weak" | "adequate" | "strong" | "unknown";
export type Band = "Low" | "Moderate" | "High" | "VeryHigh";

export type PhaseState = "pending" | "running" | "done" | "error";

export interface PhaseInfo {
  state: PhaseState;
  started_at: string | null;
  completed_at: string | null;
  task_id: string | null;
  error: string | null;
  detail: string | null;
  progress: number | null;
}

export const PHASE_KEYS = ["scoping", "scenarios", "evidence", "analysis", "score"] as const;
export type PhaseKey = (typeof PHASE_KEYS)[number];

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
  // "correlation" is a sixth, non-nav key: the pure cross-correlation phase
  // (the composite "evidence" key also folds per-document extraction in).
  phases: Partial<Record<PhaseKey | "correlation", PhaseInfo>>;
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
  combined_uplift: number;
  meta_uplift_raw: number;
  weakness_uplift_raw: number;
  effectiveness_downgrades: string[];
  rationale: string;
}

export interface AggregateScoreRead {
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
export const LEVEL_LABELS: Record<number, string> = { 1: "Low", 2: "Moderate", 3: "High", 4: "VeryHigh" };
