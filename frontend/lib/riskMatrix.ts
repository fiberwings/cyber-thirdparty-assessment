import { Band } from "./types";

// Mirrors backend/app/scoring/tables.py RISK_MATRIX — rows are impact 1..4,
// columns are likelihood 1..4. backend/tests/test_frontend_matrix_sync.py
// asserts the two tables are identical; edit both together.
export const RISK_MATRIX: Band[][] = [
  // L=1         L=2          L=3         L=4
  ["Low",      "Low",        "Moderate", "Moderate"],   // I=1
  ["Low",      "Moderate",   "Moderate", "High"],       // I=2
  ["Moderate", "Moderate",   "High",     "VeryHigh"],   // I=3
  ["Moderate", "High",       "VeryHigh", "VeryHigh"],   // I=4
];

export function bandFor(impact: number, likelihood: number): Band {
  return RISK_MATRIX[impact - 1][likelihood - 1];
}

export const LEVELS = [1, 2, 3, 4] as const;
// Top-to-bottom row order: Very High impact sits at the top of the matrix.
export const IMPACT_ROWS = [4, 3, 2, 1] as const;

export const cellKey = (impact: number, likelihood: number) => `${impact}-${likelihood}`;

// Centre of cell (impact, likelihood) as a percentage of the 4×4 cell block;
// y grows downward, matching SVG coordinates.
export function cellCenter(impact: number, likelihood: number): { x: number; y: number } {
  return { x: (likelihood - 0.5) * 25, y: (4.5 - impact) * 25 };
}
