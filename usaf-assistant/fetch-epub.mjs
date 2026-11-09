// Fetches publications from e-Publishing in a real (headless) browser and prints a JSON fingerprint for each:
//   node fetch-epub.mjs [--save DIR] URL [URL...]   ->  [{url, status, type, len, sha256, last_modified, etag, saved?}]
// Why a browser: e-Publishing sits behind Akamai bot protection that answers curl, urllib and Playwright's own request API with 403 from this
// server, but serves a normal browser session. The PDF is fetched with fetch() from inside the static host's own origin, so it is byte-exact.
// Needs @playwright/test (tests/node_modules, or PLAYWRIGHT_TEST_DIR) and the Chromium that Playwright installed (PLAYWRIGHT_CHROMIUM overrides the path).
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { writeFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const testsDir = process.env.PLAYWRIGHT_TEST_DIR || join(resolve(here, ".."), "tests");
const { chromium } = createRequire(join(testsDir, "package.json"))("@playwright/test");
const args = process.argv.slice(2);
let saveDir = null;
if (args[0] === "--save") { saveDir = args[1]; args.splice(0, 2); }
function findChromium() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM;
  for (const root of [process.env.PLAYWRIGHT_BROWSERS_PATH, `${process.env.HOME}/.cache/ms-playwright`, "/root/.cache/ms-playwright", "/home/noahflix/.cache/ms-playwright"]) {
    if (!root || !existsSync(root)) continue;
    const dirs = readdirSync(root).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse();
    for (const d of dirs) { const p = join(root, d, "chrome-linux64", "chrome"); if (existsSync(p)) return p; }
  }
  return "";
}
const chrome = findChromium();
if (!chrome) { console.error("Chromium not found (set PLAYWRIGHT_CHROMIUM)"); process.exit(2); }

const browser = await chromium.launch({ executablePath: chrome, args: ["--no-sandbox"] });
const out = [];
try {
  const ctx = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", locale: "en-US" });
  const page = await ctx.newPage();
  await page.goto("https://www.e-publishing.af.mil/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.goto("https://static.e-publishing.af.mil/robots.txt", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  for (const url of args) {
    try {
      const r = await page.evaluate(async (u) => {
        const x = await fetch(u, { cache: "no-store" });
        const buf = new Uint8Array(await x.arrayBuffer());
        let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        return { status: x.status, type: x.headers.get("content-type"), last_modified: x.headers.get("last-modified"), etag: x.headers.get("etag"), b64: x.status === 200 ? btoa(bin) : "" };
      }, url);
      const rec = { url, status: r.status, type: r.type, last_modified: r.last_modified, etag: r.etag };
      if (r.status === 200 && (r.type || "").includes("pdf")) {
        const buf = Buffer.from(r.b64, "base64");
        rec.len = buf.length; rec.sha256 = createHash("sha256").update(buf).digest("hex");
        if (saveDir) { const f = join(saveDir, url.split("/").pop()); writeFileSync(f, buf); rec.saved = f; }
      }
      out.push(rec);
    } catch (e) { out.push({ url, error: String(e).slice(0, 160) }); }
  }
} finally { await browser.close(); }
console.log(JSON.stringify(out));
