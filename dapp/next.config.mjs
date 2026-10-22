// Public build variables become browser code. Refuse private Enoki keys before bundling.
if (process.env.NEXT_PUBLIC_ENOKI_API_KEY && !process.env.NEXT_PUBLIC_ENOKI_API_KEY.startsWith("enoki_public_")) {
  throw new Error("NEXT_PUBLIC_ENOKI_API_KEY must be an Enoki public app key; private keys must never enter a frontend build.");
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  transpilePackages: ["@mysten/dapp-kit-react"],
};

export default nextConfig;
