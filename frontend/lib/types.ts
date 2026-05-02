// Hand-mirrored from backend/app/schemas/api.py — kept narrow on purpose.

export type Coverage = "none" | "partial" | "full";
export type Effectiveness = "weak" | "adequate" | "strong" | "unknown";
export type Band = "Low" | "Moderate" | "High" | "VeryHigh";

export interface Assessment {
  id: number;
  vendor_name: string;
  status: string;
  current_phase: string;
  force_continued: boolean;
  model_overrides: Record<string, string>;
  created_at: string;
  updated_at: string;
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

export interface ControlAssessmentRead {
  id: number;
  coverage: Coverage;
  effectiveness: Effectiveness;
  rationale: string;
  is_locked_by_user: boolean;
  citations: CitationRead[];
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
  meta_uplift: number;
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
  user_edited: boolean;
}

export interface MetaIssueRead {
  id: number;
  kind: string;
  target_ref: string;
  weight: number;
  rationale: string;
  scenario_code: string | null;
}

export interface ReportOut {
  assessment: Assessment;
  description: DescriptionRead | null;
  documents: DocumentRead[];
  scenarios: ScenarioScoreRead[];
  weaknesses: WeaknessRead[];
  meta_issues: MetaIssueRead[];
  aggregate: AggregateScoreRead;
}

export interface ModelProfile {
  name: string;
  default_model: string;
  alternatives: string[];
}

export const LEVEL_NAMES = ["Low", "Moderate", "High", "VeryHigh"] as const;
export const LEVEL_LABELS: Record<number, string> = { 1: "Low", 2: "Moderate", 3: "High", 4: "VeryHigh" };
