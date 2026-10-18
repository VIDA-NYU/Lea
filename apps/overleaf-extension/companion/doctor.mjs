import { spawnSync } from "node:child_process";
import { loadDotEnv } from "./config.mjs";
import { fetchAdapterHealth } from "./leaApiClient.mjs";

const dotenv = loadDotEnv();
const settings = {leaApiBaseUrl:process.env.LEA_API_BASE_URL||'http://127.0.0.1:8001'};
const checks = [];

checkCommand("node", ["--version"]);
checks.push({ok:Number(process.versions.node.split('.')[0])>=22,label:'Node 22.13+ for the adapter-owned worker',detail:process.versions.node});
checkUrl("Lea adapter URL", settings.leaApiBaseUrl || "http://127.0.0.1:8001");

const health = await fetchAdapterHealth({ baseUrl: settings.leaApiBaseUrl || "http://127.0.0.1:8001",
  fetchImpl: (url, options) => fetch(url, { ...options, signal: AbortSignal.timeout(1500) }) });
if (health.ok) {
  const capability = health.body?.capabilities?.lea_status;
  checks.push({ ok: capability?.version === 1 && capability?.independent_checks === false,
    label: "Live Lea Status v1", detail: capability?.version === 1
      ? (capability.admission_enabled ? "enabled; independent evaluator retired" : "new admissions disabled")
      : "restart the updated adapter and reload the extension" });
} else {
  console.log("• Live Lea Status capability will be checked when the adapter starts.");
}
console.log("Overleaf Lea Formalizer doctor\n");
console.log(`${dotenv.loaded ? "✓" : "•"} root .env ${dotenv.loaded ? `loaded from ${dotenv.path}` : "not found; using shell/settings only"}`);
for (const check of checks) {
  console.log(`${check.ok ? "✓" : "✗"} ${check.label}${check.detail ? `: ${check.detail}` : ""}`);
}

const failed = checks.filter((check) => !check.ok);
if (failed.length > 0) {
  console.log("\nFix the failed checks above, then run `npm run doctor` again.");
  process.exitCode = 1;
} else {
  console.log("\nAll checks passed. Start with `npm start`.");
}

function checkCommand(command, args) {
  const result = spawnSync(command, args, { encoding: "utf8" });
  checks.push({
    ok: result.status === 0,
    label: `${command} available`,
    detail: result.status === 0
      ? firstLine(result.stdout || result.stderr)
      : "not found on PATH"
  });
}

function checkUrl(label, value) {
  let ok = false;
  try {
    const parsed = new URL(value);
    ok = ["http:", "https:"].includes(parsed.protocol);
  } catch {
    ok = false;
  }
  checks.push({
    ok,
    label,
    detail: ok ? value : `${value || "unset"} (must be absolute http(s) URL)`
  });
}

function firstLine(text) {
  return String(text).trim().split(/\r?\n/)[0] || "ok";
}
