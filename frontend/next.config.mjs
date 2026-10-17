/** @type {import('next').NextConfig} */
const isDev = process.env.NODE_ENV === "development";

const nextConfig = {
  // Production: static files served by the FastAPI backend from one Cloud Run service.
  output: isDev ? undefined : "export",
  images: { unoptimized: true },
  reactStrictMode: true,
  // Local development: forward API calls to the backend on :8080.
  ...(isDev && {
    async rewrites() {
      return [{ source: "/api/:path*", destination: "http://localhost:8080/api/:path*" }];
    },
  }),
};

export default nextConfig;
