#!/usr/bin/env node
/**
 * Vendor the minimal AAVA core needed by `aava-exec` into ./core.
 *
 * Unlike the VS Code extension (which regenerates core/ at build and bakes it
 * into the VSIX), a GitHub Action runs the repo's COMMITTED files at the used
 * ref — so core/ is checked in here and pinned to a core tag.
 *
 * Copies from a sibling aava-plugin-core checkout (override with AAVA_CORE_SRC):
 *   bin/aava-exec, lib/, VERSION
 * (exec only needs lib/transport + lib/runs + lib/cache — no framework/vendor/prompts.)
 */
import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..");
const coreSrc = process.env.AAVA_CORE_SRC || join(repoRoot, "..", "aava-plugin-core");
const dest = join(repoRoot, "core");

if (!existsSync(join(coreSrc, "VERSION")) || !existsSync(join(coreSrc, "bin", "aava-exec"))) {
  console.error(`[sync-core] '${coreSrc}' is not an aava-plugin-core checkout with bin/aava-exec.`);
  console.error(`[sync-core] set AAVA_CORE_SRC to a valid core path.`);
  process.exit(1);
}

rmSync(dest, { recursive: true, force: true });
mkdirSync(join(dest, "bin"), { recursive: true });

cpSync(join(coreSrc, "bin", "aava-exec"), join(dest, "bin", "aava-exec"));
cpSync(join(coreSrc, "lib"), join(dest, "lib"), { recursive: true });
cpSync(join(coreSrc, "VERSION"), join(dest, "VERSION"));

console.log(`[sync-core] vendored core (bin/aava-exec + lib + VERSION) from ${coreSrc} -> ${dest}`);
