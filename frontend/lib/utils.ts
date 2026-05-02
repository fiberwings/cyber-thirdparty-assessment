import { Band } from "./types";

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
