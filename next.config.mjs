/** @type {import('next').NextConfig} */
const nextConfig = {
  experimental: {
    // enables streaming responses from API routes
    serverActions: { bodySizeLimit: '2mb' },
  },
  env: {
    FLYIO_API_URL: process.env.FLYIO_API_URL,
  },
}

export default nextConfig
