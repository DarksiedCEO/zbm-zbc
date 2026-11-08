// ZBM stub: sharp (and its LGPL-3.0 libvips binaries) is deliberately not installed in the dashboard.
// next.config.mjs sets images.unoptimized: true, so Next.js never loads an image optimiser. Anything that does
// require('sharp') gets this explicit error instead of a silent no-op.
throw new Error(
  "sharp is intentionally not installed in the ZBM dashboard (images.unoptimized: true; see docs/runbooks/dependency-ownership.md)",
);
