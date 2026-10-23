// Patches the mirrored app's next.config.ts so Turbopack may follow the node_modules symlink into
// frontend/node_modules (outside the mirror's project root). Only the mirror under out/app is
// touched; frontend/ itself is never modified or built.
import fs from "node:fs";
import path from "node:path";
import { APP_DIR, REPO_DIR } from "./lib/paths.mjs";

const config = path.join(APP_DIR, "next.config.ts");
const modules = fs.realpathSync(path.join(REPO_DIR, "frontend", "node_modules"));
let src = fs.readFileSync(config, "utf8");
const anchor = "const nextConfig: NextConfig = {";
if (!src.includes(anchor)) throw new Error(`${config}: cannot find "${anchor}" to add turbopackAdditionalRoots`);
if (!src.includes("turbopackAdditionalRoots")) {
  src = src.replace(anchor, `${anchor}\n  experimental: { turbopackAdditionalRoots: { frontendModules: { path: ${JSON.stringify(modules)} } } },`);
  fs.writeFileSync(config, src);
}
console.log(`[video] mirror next.config.ts: turbopackAdditionalRoots -> ${modules}`);
