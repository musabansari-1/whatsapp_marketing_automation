import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Deployed to Vercel, never Cloud Run ([D12]), so no standalone output target.
  poweredByHeader: false,
};

export default nextConfig;
