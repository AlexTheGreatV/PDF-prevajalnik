import type { NextConfig } from "next";

const ocrSmokeContentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "img-src 'self' data: blob:",
  "style-src 'self' 'unsafe-inline'",
  "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
  "worker-src 'self'",
  "connect-src 'self'",
].join("; ");

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/ocr-smoke/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            value: ocrSmokeContentSecurityPolicy,
          },
        ],
      },
    ];
  },
};

export default nextConfig;
