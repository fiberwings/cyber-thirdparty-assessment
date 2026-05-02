import { Band, ScenarioRead, ScenarioScoreRead, WeaknessRead } from "./types";

export function bandRank(band: Band): number {
  switch (band) {
    case "VeryHigh": return 4;
    case "High": return 3;
    case "Moderate": return 2;
    case "Low": return 1;
  }
}

export function severityRank(severity: WeaknessRead["severity"]): number {
  switch (severity) {
    case "critical": return 4;
    case "high": return 3;
    case "medium": return 2;
    case "low": return 1;
  }
}

// Highest residual risk first; tiebreak on residual impact × likelihood, then code.
export function compareScenariosByRisk(a: ScenarioRead, b: ScenarioRead): number {
  const db = bandRank(b.score_band) - bandRank(a.score_band);
  if (db !== 0) return db;
  const sb = b.residual_impact * b.residual_likelihood - a.residual_impact * a.residual_likelihood;
  if (sb !== 0) return sb;
  return a.code.localeCompare(b.code);
}

export function compareScenarioScoresByRisk(a: ScenarioScoreRead, b: ScenarioScoreRead): number {
  const db = bandRank(b.band) - bandRank(a.band);
  if (db !== 0) return db;
  const sb = b.residual_impact * b.residual_likelihood - a.residual_impact * a.residual_likelihood;
  if (sb !== 0) return sb;
  return a.code.localeCompare(b.code);
}

export function compareWeaknessesBySeverity(a: WeaknessRead, b: WeaknessRead): number {
  const ds = severityRank(b.severity) - severityRank(a.severity);
  if (ds !== 0) return ds;
  return a.id - b.id;
}

export function bandColor(band: Band): string {
  switch (band) {
    case "Low": return "bg-risk-low";
    case "Moderate": return "bg-risk-moderate";
    case "High": return "bg-risk-high";
    case "VeryHigh": return "bg-risk-veryhigh";
  }
}

export function bandTextColor(band: Band): string {
  switch (band) {
    case "Low": return "text-risk-low";
    case "Moderate": return "text-risk-moderate";
    case "High": return "text-risk-high";
    case "VeryHigh": return "text-risk-veryhigh";
  }
}

export function bandLabel(band: Band): string {
  return band === "VeryHigh" ? "Very High" : band;
}

export function formatPercent(x: number): string {
  return `${Math.round(x * 100)}%`;
}
