import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
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
    },
  },
  plugins: [],
};

export default config;
