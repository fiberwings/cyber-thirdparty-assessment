import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    // lib/ returns class names as strings (bandColor, phaseDotColor,
    // SEVERITY_STYLES) — unscanned, those classes never reach the bundle.
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#f6f7f9",
          100: "#eceef2",
          200: "#d6dae2",
          300: "#b3bac8",
          400: "#8993a8",
          500: "#6a7589",
          600: "#525c6f",
          700: "#3f485a",
          800: "#283041",
          900: "#1a2030",
          950: "#0f131e",
        },
        risk: {
          low:       "#16a34a",
          moderate:  "#eab308",
          high:      "#ea580c",
          veryhigh:  "#b91c1c",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "Inter", "Segoe UI", "Helvetica Neue", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px 0 rgba(15,19,30,0.04), 0 1px 3px 0 rgba(15,19,30,0.05)",
      },
      // AI activity indicator (components/AiActivity.tsx). Every use pairs
      // with motion-reduce:animate-none.
      keyframes: {
        "ai-orbit": { from: { transform: "rotate(0deg)" }, to: { transform: "rotate(360deg)" } },
        "ai-breathe": {
          "0%, 100%": { transform: "scale(0.85)", opacity: "0.55" },
          "50%": { transform: "scale(1.05)", opacity: "1" },
        },
        "ai-shimmer": { from: { transform: "translateX(-100%)" }, to: { transform: "translateX(300%)" } },
        "ai-fade": { from: { opacity: "0", transform: "translateY(3px)" }, to: { opacity: "1", transform: "translateY(0)" } },
      },
      animation: {
        "ai-orbit": "ai-orbit 1.6s linear infinite",
        "ai-orbit-slow": "ai-orbit 2.8s linear infinite reverse",
        "ai-breathe": "ai-breathe 1.8s ease-in-out infinite",
        "ai-shimmer": "ai-shimmer 1.4s ease-in-out infinite",
        "ai-fade": "ai-fade 300ms ease-out both",
      },
    },
  },
  plugins: [],
};

export default config;
