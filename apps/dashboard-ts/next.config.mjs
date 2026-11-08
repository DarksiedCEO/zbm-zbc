/** @type {import('next').NextConfig} */
const nextConfig = {
  // The dashboard serves no optimised images (no next/image import). Next.js's optional `sharp` dependency ships
  // LGPL-3.0 libvips binaries, which the owner's licence ruling (2026-11-08) forbids; package.json overrides it with
  // the empty local stub in stubs/sharp, and this flag keeps Next.js from ever trying to load an optimiser.
  images: { unoptimized: true },
};

export default nextConfig;
