import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

// Public build variables become browser code. Refuse private Enoki keys before bundling.
if (process.env.NEXT_PUBLIC_ENOKI_API_KEY && !process.env.NEXT_PUBLIC_ENOKI_API_KEY.startsWith("enoki_public_")) {
  throw new Error("NEXT_PUBLIC_ENOKI_API_KEY must be an Enoki public app key; private keys must never enter a frontend build.");
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  // Stable input-bound build ID: repeated exports of identical source,
  // dependencies and public deployment configuration use the same route data.
  generateBuildId: async () => {
    const hash = createHash("sha256");
    const add = (directory) => {
      for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name, "en"))) {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) add(path);
        else if (entry.isFile()) { hash.update(path); hash.update(readFileSync(path)); }
      }
    };
    add("src"); if (existsSync("public")) add("public");
    for (const file of ["package.json", "pnpm-lock.yaml", "next.config.mjs"]) hash.update(readFileSync(file));
    for (const name of Object.keys(process.env).filter(k => k.startsWith("NEXT_PUBLIC_")).sort()) {
      hash.update(name); hash.update(process.env[name] ?? "");
    }
    return hash.digest("hex");
  },
  transpilePackages: ["@mysten/dapp-kit-react"],
};

export default nextConfig;
