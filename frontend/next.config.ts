import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Deployed to Cloudflare Workers (OpenNext), never Cloud Run ([D12]), so no standalone output target.
  poweredByHeader: false,
};

export default nextConfig;
