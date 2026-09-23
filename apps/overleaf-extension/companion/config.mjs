import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadEnvFile, ROOT_ENV_PATH } from "../../../scripts/env.mjs";
import { normalizeModelFamilyId } from "../../../packages/lea-model-catalog/index.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MONOREPO_ROOT = path.resolve(PROJECT_ROOT, "../..");
const DEFAULT_LEA_REPO_PATH = path.resolve(MONOREPO_ROOT, "apps", "lea-standalone", "prover");

export function loadDotEnv(projectRoot = MONOREPO_ROOT, target = process.env) {
  const envRoot = projectRoot === PROJECT_ROOT ? MONOREPO_ROOT : projectRoot;
  return loadEnvFile(path.join(envRoot, ".env"), target);
}

export function loadRootDotEnv(target = process.env) {
  return loadEnvFile(ROOT_ENV_PATH, target);
}

export function applyEnvDefaults(settings, env = process.env) {
  const leaMaxSpendUsd = normalizeOptionalNonNegativeNumber(
    env.LEA_MAX_SPEND_USD !== undefined
      ? env.LEA_MAX_SPEND_USD
      : settings.leaMaxSpendUsd,
    "leaMaxSpendUsd"
  );
  return {
    ...settings,
    leaRepoPath: resolveLeaRoot(env.LEA_REPO_PATH || env.LEA_ROOT || settings.leaRepoPath),
    leaNarrateToolSteps: normalizeBoolean(
      env.LEA_NARRATE_TOOL_STEPS !== undefined ? env.LEA_NARRATE_TOOL_STEPS : settings.leaNarrateToolSteps,
      true
    ),
    leaApiBaseUrl: env.LEA_API_BASE_URL || settings.leaApiBaseUrl || "http://127.0.0.1:8001",
    leaUiBaseUrl: env.LEA_UI_BASE_URL || settings.leaUiBaseUrl || "http://localhost:5173",
    leaProvider: normalizeModelFamilyId(env.LEA_PROVIDER || settings.leaProvider || "openai"),
    leaModel: env.LEA_MODEL || settings.leaModel || "o4-mini",
    leaMaxTurns: parseInt(env.LEA_MAX_TURNS || settings.leaMaxTurns || "20", 10),
    leaJobTimeoutSeconds: parseInt(env.LEA_JOB_TIMEOUT_SECONDS || settings.leaJobTimeoutSeconds || "900", 10),
    leaTexMirrorEnabled: normalizeBoolean(
      env.LEA_TEX_MIRROR !== undefined ? env.LEA_TEX_MIRROR : settings.leaTexMirrorEnabled,
      true
    ),
    leaPauseOnSourceIssue: settings.leaPauseOnSourceIssue === true,
    leaMaxSpendUsd
  };
}

function resolveLeaRoot(value) {
  if (!value) return DEFAULT_LEA_REPO_PATH;
  return path.isAbsolute(value) ? value : path.resolve(MONOREPO_ROOT, value);
}

export function normalizeBoolean(value, fallback = false) {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  const text = String(value).trim().toLowerCase();
  if (["true", "1", "yes", "on"].includes(text)) return true;
  if (["false", "0", "no", "off"].includes(text)) return false;
  return fallback;
}

// Reading defaults from the environment must never crash the companion at
// startup (AUDIT M8): a bad `LEA_MAX_SPEND_USD` (e.g. `abc` or `-1`) is warned
// about and treated as "no cap" rather than throwing out of createServer. The
// interactive settings path uses server.mjs's own normalizeLeaMaxSpendUsd,
// which still throws so the UI can reject the value.
function normalizeOptionalNonNegativeNumber(value, fieldName) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    console.warn(`[companion] Ignoring invalid ${fieldName}=${JSON.stringify(value)} (must be a number >= 0); treating as unset.`);
    return null;
  }
  return number;
}
