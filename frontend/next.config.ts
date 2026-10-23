import type { NextConfig } from "next";

/** The static demo for GitHub Pages (MOONWRIGHT_STATIC=1, built together with
 *  NEXT_PUBLIC_STATIC_DEMO=1 and NEXT_PUBLIC_BASE_PATH=/moonwright): a plain export of the
 *  console under the base path, with no server behind it. An export cannot carry Cache
 *  Components (Partial Prerendering) or Partial Prefetching, so only this build turns them off;
 *  the dev server and the normal build keep them. */
const STATIC_EXPORT = process.env.MOONWRIGHT_STATIC === "1";
const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

const staticExport: NextConfig = {
  output: "export",
  ...(BASE_PATH ? { basePath: BASE_PATH } : {}),
  // /moonwright/ serves out/index.html as it is; a static host has no rewrites
  trailingSlash: true,
  images: { unoptimized: true },
};

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: !STATIC_EXPORT,
  partialPrefetching: !STATIC_EXPORT,
  ...(STATIC_EXPORT ? staticExport : {}),
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
