import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        sui: {
          blue: "#6fbcf0",
          dark: "#1a2744",
        },
      },
    },
  },
  plugins: [],
};

export default config;
