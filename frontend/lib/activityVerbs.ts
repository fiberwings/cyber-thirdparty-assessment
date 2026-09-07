// Rotating "thinking" verbs per task kind — flavour for the indicator's
// headline while the structured fields (stage, units, tokens) carry the
// facts. Professional, domain-specific, present participle.
//
// `byStage` overrides the default list while that stage is active so the
// verb never contradicts what the backend says it is doing.

export interface VerbSet {
  default: string[];
  byStage?: Record<string, string[]>;
}

export const VERBS: Record<string, VerbSet> = {
  scoping_turn: {
    default: [
      "Reading the service description",
      "Mapping data flows",
      "Weighing exposure",
      "Identifying open questions",
      "Framing the scope",
      "Checking sufficiency",
      "Drafting the next question",
    ],
  },
  scenarios_generation: {
    default: [
      "Reading the scope",
      "Modelling threat paths",
      "Drafting risk scenarios",
      "Rating inherent impact",
      "Rating inherent likelihood",
    ],
    byStage: {
      "Selecting controls": [
        "Selecting expected controls",
        "Aligning to the control catalogue",
        "Weighting controls by relevance",
        "Cross-checking coverage",
        "Explaining control rationale",
      ],
    },
  },
  document_extraction: {
    default: [
      "Reading the evidence",
      "Parsing sections",
      "Locating control statements",
      "Spotting exceptions",
      "Noting qualified opinions",
      "Drafting findings",
      "Anchoring citations",
    ],
    byStage: {
      "Enumerating findings": ["Scanning for candidate findings", "Listing exceptions", "Marking section anchors"],
      "Detailing findings": ["Detailing a finding", "Quoting the evidence", "Rating severity", "Anchoring citations"],
      "Extracting sections": ["Reading a questionnaire window", "Reading answers", "Spotting gaps in the answers"],
      "Attestation profile": ["Reading the report scope", "Extracting the opinion", "Dating the period", "Listing carve-outs"],
    },
  },
  cross_correlation: {
    default: [
      "Reviewing candidates against the bundle",
      "Confirming findings",
      "Merging duplicates",
      "Clustering by theme",
      "Mapping to controls",
      "Proposing emergent scenarios",
    ],
    byStage: {
      "Attestation checks": ["Checking attestation freshness", "Checking scope and opinion", "Applying deterministic checks"],
      "Confirming candidates": ["Reading the whole bundle", "Confirming a finding", "Weighing contradicting evidence", "Dropping unsupported candidates"],
      "Merging duplicates": ["Merging duplicate findings", "Reconciling overlapping quotes", "Consolidating evidence"],
      "Correlating clusters": ["Clustering by theme", "Mapping a cluster to controls", "Spotting emergent risks", "Proposing emergent scenarios"],
      "Accuracy floor": ["Applying the severity floor", "Checking unmatched high findings"],
    },
  },
  gap_analysis: {
    default: [
      "Reading the evidence bundle",
      "Testing control coverage",
      "Judging effectiveness",
      "Citing supporting evidence",
      "Recording unknowns",
      "Weighing residual gaps",
      "Reconciling scenarios",
    ],
  },
  gap_analysis_control: {
    default: [
      "Re-reading the evidence",
      "Re-testing coverage",
      "Re-judging effectiveness",
      "Refreshing citations",
      "Recording unknowns",
    ],
  },
  narratives: {
    default: [
      "Reading scenario scores",
      "Explaining the drivers",
      "Writing the residual-risk story",
      "Citing the evidence",
      "Balancing the tone",
    ],
    byStage: {
      "Executive summary": ["Weighing the overall verdict", "Distilling key risks", "Summarising control posture", "Writing for the board"],
    },
  },
  executive_summary: {
    default: [
      "Weighing the overall verdict",
      "Distilling key risks",
      "Summarising control posture",
      "Noting open items",
      "Writing for the board",
    ],
  },
  attestation_profile: {
    default: [
      "Reading the report scope",
      "Extracting the opinion",
      "Listing exceptions",
      "Dating the period",
      "Typing the profile",
    ],
  },
};

const GENERIC: string[] = ["Working", "Reading the evidence", "Reasoning", "Drafting"];

export function verbsFor(kind: string | undefined, stage: string | undefined): string[] {
  const set = kind ? VERBS[kind] : undefined;
  if (!set) return GENERIC;
  if (stage && set.byStage?.[stage]) return set.byStage[stage];
  return set.default;
}

// Deterministic pick from elapsed time: rotates every `periodS` seconds and
// resumes at the same word after a remount or a navigation.
export function verbAt(list: string[], elapsedS: number, periodS = 2.5): { verb: string; index: number } {
  if (list.length === 0) return { verb: "Working", index: 0 };
  const index = Math.floor(Math.max(0, elapsedS) / periodS) % list.length;
  return { verb: list[index], index };
}
