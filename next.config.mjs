/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000"

const nextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
  async rewrites() {
    return [
      {
        // Proxy all /api/v1/* requests to the Python backend
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
    ]
  },
}

export default nextConfig
