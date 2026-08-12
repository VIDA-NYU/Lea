import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createHash } from "node:crypto";
import {
  LEA_MODEL_OPTIONS,
  buildOverleafDocumentUrl,
  buildSettingsResponse,
  createServer,
  ensureStartupLeaRuntime,
  handleChatInterrupt,
  handleChatMessage,
  handleChatPoll,
  handleChatSession,
  handleFormalize,
  handleGetModelCatalog,
  handleGetModelRequirements,
  handleGetStatuses,
  handleGetUsage,
  handleGithubImportConfirm,
  handleGithubImportPreview,
  handleGithubImportStatus,
  handleGithubTokenUpdate,
  handleLeanPaneManifest,
  handleMirrorTex,
  handleProjectIdentity,
  handleProjectIdentityPreview,
  handleProjectIdentityUpdate,
  handleProjectExport,
  handleProjectGraph,
  handleProjectBlueprintGenerate,
  handleShareSetRemote,
  handleShareStatus,
  handleSharePush,
  handleStub,
  handleUpdateLeaSettings,
  recoverFormalizedStatusFromTargetPath,
  recoverInterruptedJobs,
  resolveProofOutcome,
  validateLeaRepo
} from "../companion/server.mjs";
import {
  buildLeaProjectMarkdownPath,
  buildLeaWorkspacePath,
  slugProjectId
} from "../shared/leanStub.mjs";
import { hashFormalizationInput, hashTargetText } from "../shared/theoremParser.mjs";

test("buildOverleafDocumentUrl builds the canonical public-Overleaf URL", () => {
  assert.equal(
    buildOverleafDocumentUrl("abc123"),
    "https://www.overleaf.com/project/abc123"
  );
  // Empty / unknown ids yield no link (the session shows the badge without a click target).
  assert.equal(buildOverleafDocumentUrl(""), null);
  assert.equal(buildOverleafDocumentUrl("unknown"), null);
  assert.equal(buildOverleafDocumentUrl(null), null);
});

test("validates a Lea repo and derives its workspace", async () => {
  const leaRepo = await makeLeaRepo();

  assert.deepEqual(await validateLeaRepo(leaRepo), {
    ok: true,
    leaWorkspacePath: path.join(leaRepo, "workspace")
  });
});

test("startup records the derived Lea workspace without creating root Lean files", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });

  const result = await ensureStartupLeaRuntime(state);

  assert.equal(result.ok, true);
  assert.equal(state.settings.leaWorkspacePath, buildLeaWorkspacePath(leaRepo));
  assert.equal(await fileExists(path.join(path.dirname(state.settingsPath), "lean-toolchain")), false);
  assert.equal(await fileExists(path.join(path.dirname(state.settingsPath), "lakefile.lean")), false);
});

test("createServer recovers from a corrupt jobs.json instead of crashing (AUDIT H1)", async () => {
  const appDir = await fs.mkdtemp(path.join(os.tmpdir(), "overleaf-lean-corrupt-"));
  const jobsPath = path.join(appDir, "jobs.json");
  // A truncated/torn state file — exactly what a crash mid-write could leave.
  await fs.writeFile(jobsPath, '{ "job-1": { "status": "in_prog', "utf8");

  // A complete env so startup produces no env patch and never touches the real
  // root .env during the test.
  const env = {
    LEA_ROOT: path.join(appDir, "prover"),
    LEA_API_BASE_URL: "http://127.0.0.1:8001",
    LEA_UI_BASE_URL: "http://localhost:5173",
    LEA_PROVIDER: "openai",
    LEA_MODEL: "o4-mini",
    LEA_MAX_TURNS: "20",
    LEA_NARRATE_TOOL_STEPS: "true",
    LEA_MAX_SPEND_USD: "5",
    LEA_JOB_TIMEOUT_SECONDS: "900"
  };

  // The old readJson rethrew any JSON parse error, so this call would throw and
  // the companion would refuse to boot until the file was deleted by hand.
  const server = await createServer({
    settingsPath: path.join(appDir, "settings.json"),
    jobsPath,
    chatSessionsPath: path.join(appDir, "chatSessions.json"),
    env
  });
  server.close?.();

  // Did not throw; jobs started from a clean slate.
  assert.deepEqual(server.leaState.jobs, {});
  // The corrupt bytes were preserved for forensics rather than silently lost.
  const entries = await fs.readdir(appDir);
  assert.ok(
    entries.some((name) => name.startsWith("jobs.json.corrupt-")),
    "corrupt jobs.json should be moved aside to a .corrupt-* backup"
  );
});

test("builds Lea-compatible Overleaf project slugs and markdown paths", () => {
  const long = `9${"a".repeat(120)}`;
  assert.equal(slugProjectId("abc/123?x"), "abc_123_x");
  assert.equal(slugProjectId("-abc"), "project_-abc");
  assert.equal(slugProjectId("_abc"), "project__abc");
  assert.equal(slugProjectId(""), "unknown");
  assert.equal(slugProjectId(long).length, 80);
  assert.match(slugProjectId("!!!"), /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/);
  assert.equal(
    buildLeaProjectMarkdownPath({ leaRepoPath: "/tmp/lea", overleafProjectId: "abc/123?x" }),
    path.join("/tmp/lea", "workspace", "projects", "abc_123_x.md")
  );
});

test("project identity endpoints proxy adapter state without creating on missing get", async () => {
  const calls = [];
  const state = await makeState({
    leaRepoPath: await makeLeaRepo(),
    fetchImpl: async (url, options = {}) => {
      calls.push({ url: String(url), options, body: options.body ? JSON.parse(options.body) : null });
      if (String(url).endsWith("/api/projects/by-slug/project-1/identity") && options.method === "GET") {
        return jsonResponse(404, { detail: "No project" });
      }
      if (String(url).endsWith("/api/projects/namespace-preview")) {
        return jsonResponse(200, { project_name: "Fourier Notes", namespace: "Lea.FourierNotes", available: true, suggestions: [] });
      }
      if (String(url).endsWith("/api/projects/by-slug/project-1/identity") && options.method === "PUT") {
        return jsonResponse(200, {
          identity: {
            projectId: "p1",
            slug: "project-1",
            projectName: "Fourier Notes",
            namespace: "Lea.FourierNotes",
            namespaceEditable: true,
            repoPath: "proofs/Lea/FourierNotes",
            hasRecordedProofs: false,
            exists: true
          }
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    }
  });

  const missing = await handleProjectIdentity({ overleafProjectId: "project-1" }, state);
  assert.equal(missing.statusCode, 200);
  assert.equal(missing.body.identity.exists, false);

  const preview = await handleProjectIdentityPreview({ overleafProjectId: "project-1", projectName: "Fourier Notes" }, state);
  assert.equal(preview.body.namespace, "Lea.FourierNotes");

  state.jobs.old = {
    jobId: "old",
    jobKey: "project-1:theorem:old",
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    projectName: "Old Name",
    projectNamespace: "Lea.OldName"
  };
  const update = await handleProjectIdentityUpdate({
    overleafProjectId: "project-1",
    projectName: "Fourier Notes",
    mode: "rename-namespace",
    namespace: "Lea.FourierNotes",
    createIfMissing: true
  }, state);
  assert.equal(update.body.identity.projectName, "Fourier Notes");
  assert.equal(state.jobs.old.projectName, "Fourier Notes");
  assert.equal(state.jobs.old.projectNamespace, "Lea.OldName");
  assert.equal(calls.find((call) => call.options.method === "PUT").body.create_if_missing, true);
});

test("namespace migration rebases cached job artifact pointers without rewriting history", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: async (url, options = {}) => {
      if (String(url).endsWith("/api/projects/by-slug/project-1/identity") && options.method === "PUT") {
        return jsonResponse(200, {
          identity: {
            projectId: "p1",
            slug: "project-1",
            projectName: "Fourier Notes",
            namespace: "Lea.FourierNotes",
            namespaceEditable: true,
            repoPath: "proofs/Lea/FourierNotes",
            hasRecordedProofs: true,
            exists: true
          },
          migration: {
            oldNamespace: "Lea.OldName",
            newNamespace: "Lea.FourierNotes"
          }
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    }
  });
  const oldAbsolute = path.join(leaRepo, "workspace", "proofs", "Lea", "OldName", "proof.lean");
  state.jobs.old = {
    jobId: "old",
    jobKey: "project-1:theorem:proof",
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    projectName: "Old Name",
    projectNamespace: "Lea.OldName",
    moduleName: "Lea.OldName.proof",
    recordedProofPath: "workspace/proofs/Lea/OldName/proof.lean",
    absolutePath: oldAbsolute,
    stubbedTheoremUses: [{
      moduleName: "Lea.OldName.helper",
      relativePath: "workspace/proofs/Lea/OldName/helper.lean",
      absolutePath: path.join(leaRepo, "workspace", "proofs", "Lea", "OldName", "helper.lean")
    }]
  };

  const update = await handleProjectIdentityUpdate({
    overleafProjectId: "project-1",
    projectName: "Fourier Notes",
    mode: "rename-namespace",
    namespace: "Lea.FourierNotes",
    expectedNamespace: "Lea.OldName"
  }, state);

  assert.equal(update.statusCode, 200);
  assert.equal(state.jobs.old.projectNamespace, "Lea.FourierNotes");
  assert.equal(state.jobs.old.moduleName, "Lea.FourierNotes.proof");
  assert.equal(state.jobs.old.recordedProofPath, "workspace/proofs/Lea/FourierNotes/proof.lean");
  assert.equal(
    state.jobs.old.absolutePath,
    path.join(leaRepo, "workspace", "proofs", "Lea", "FourierNotes", "proof.lean")
  );
  assert.equal(state.jobs.old.stubbedTheoremUses[0].moduleName, "Lea.FourierNotes.helper");
  assert.equal(
    state.jobs.old.stubbedTheoremUses[0].relativePath,
    "workspace/proofs/Lea/FourierNotes/helper.lean"
  );
});

test("settings response includes model families and key status", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaMaxSpendUsd: 0.5,
    env: { OPENAI_API_KEY: "env-openai", ANTHROPIC_API_KEY: "env-anthropic" }
  });
  state.jobs.usage = makeUsageJob({
    jobId: "usage",
    projectId: "project-1",
    inputTokens: 100,
    outputTokens: 50,
    costUsd: 0.125
  });

  const response = await buildSettingsResponse(state);

  assert.deepEqual(response.leaModelOptions, LEA_MODEL_OPTIONS);
  assert.deepEqual(response.leaModelOptions.map((model) => model.value), [
    "o4-mini",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-4o",
    "gemini/gemini-3.1-pro-preview",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-sonnet-4-6"
  ]);
  assert.equal(response.leaModelOptions.find((model) => model.value === "o4-mini").family, "openai");
  assert.equal(response.leaModelOptions.find((model) => model.value === "gemini/gemini-2.5-pro").family, "google");
  assert.equal(response.leaProviderKeys.openai.configured, true);
  assert.equal(response.leaProviderKeys.google.configured, false);
  assert.equal(response.leaProviderKeys.anthropic.configured, true);
  assert.equal(response.leaTexMirrorEnabled, true);
  assert.equal(response.leaMaxSpendUsd, 0.5);
  assert.equal(response.leaCurrentSpendUsd, 0.125);
});

test("settings response reloads model selection from env file", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaModel: "o4-mini",
    env: { OPENAI_API_KEY: "openai-key", ANTHROPIC_API_KEY: "anthropic-key" }
  });
  await fs.writeFile(
    state.envPath,
    "LEA_MODEL=anthropic/claude-sonnet-4-6\nANTHROPIC_API_KEY=anthropic-key\n",
    "utf8"
  );

  const response = await buildSettingsResponse(state);

  assert.equal(response.leaModel, "anthropic/claude-sonnet-4-6");
  assert.equal(response.leaProvider, "anthropic");
});

test("model catalog and requirements proxy the adapter's exhaustive source", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapter = makeAdapterStore({
    models: [
      { value: "mistral/mistral-large-latest", label: "mistral/mistral-large-latest", provider: "mistral" },
      { value: "ollama/llama3", label: "ollama/llama3", provider: "ollama" }
    ]
  });
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeAdapterFetch(adapter, calls)
  });

  const catalog = await handleGetModelCatalog(state);
  const requirements = await handleGetModelRequirements("mistral/mistral-large-latest", state);

  assert.equal(catalog.statusCode, 200);
  assert.equal(catalog.body.source, "adapter");
  assert.deepEqual(catalog.body.models.map((model) => model.value), [
    "mistral/mistral-large-latest",
    "ollama/llama3"
  ]);
  assert.equal(requirements.body.provider, "mistral");
  assert.equal(requirements.body.required_keys[0].env, "MISTRAL_API_KEY");
  assert.ok(calls.some((call) => call.url.endsWith("/api/models")));
  assert.ok(calls.some((call) => call.url.includes("/api/models/requirements")));
});

test("model catalog fallback preserves a configured custom model", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaModel: "custom/acme-proof-model",
    fetchImpl: async () => { throw new Error("adapter offline"); }
  });

  const catalog = await handleGetModelCatalog(state);

  assert.equal(catalog.body.degraded, true);
  assert.equal(catalog.body.models[0].value, "custom/acme-proof-model");
});

test("settings forward dynamically required provider keys by environment name", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapter = makeAdapterStore();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: {},
    fetchImpl: makeAdapterFetch(adapter, calls)
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "mistral/mistral-large-latest",
    leaMaxTurns: 20,
    leaApiKeys: { MISTRAL_API_KEY: "mistral-secret" }
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.leaModel, "mistral/mistral-large-latest");
  const put = calls.find((call) => call.method === "PUT");
  assert.deepEqual(put.body.api_keys.MISTRAL_API_KEY, { value: "mistral-secret" });
  assert.equal(state.env.MISTRAL_API_KEY, "mistral-secret");
});

test("settings accept custom model ids and surface adapter credential errors", async () => {
  const leaRepo = await makeLeaRepo();
  const customAdapter = makeAdapterStore({
    api_keys: { ANTHROPIC_API_KEY: keyStatus("akey") }
  });
  const customState = await makeState({
    leaRepoPath: leaRepo,
    env: { ANTHROPIC_API_KEY: "anthropic-key" },
    fetchImpl: makeAdapterFetch(customAdapter)
  });

  const customModel = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "anthropic/claude-does-not-exist",
    leaMaxTurns: 20
  }, customState);
  const rejectingAdapter = makeAdapterStore({
    reject: {
      message: "An API key (Google) is required before saving this model.",
      field: "api_keys.GOOGLE_API_KEY"
    }
  });
  const missingKeyState = await makeState({
    leaRepoPath: leaRepo,
    env: {},
    fetchImpl: makeAdapterFetch(rejectingAdapter)
  });
  const missingGeminiKey = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gemini/gemini-2.5-pro",
    leaMaxTurns: 20
  }, missingKeyState);

  assert.equal(customModel.statusCode, 200);
  assert.equal(customModel.body.leaModel, "anthropic/claude-does-not-exist");
  assert.equal(missingGeminiKey.statusCode, 422);
  assert.equal(missingGeminiKey.body.error, "adapter_settings_rejected");
  assert.equal(missingGeminiKey.body.field, "api_keys.GOOGLE_API_KEY");
});

test("settings save supported models when their family key is configured", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo, env: { OPENAI_API_KEY: "openai-key", GEMINI_API_KEY: "gemini-key", ANTHROPIC_API_KEY: "anthropic-key" } });

  const openAiResult = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gpt-5.4-mini",
    leaMaxTurns: 34,
    leaMaxSpendUsd: 9.5,
    leaTheoremTranslationMaxRetries: 8
  }, state);
  const geminiResult = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gemini/gemini-2.5-flash",
    leaMaxTurns: 12
  }, state);
  const anthropicResult = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "anthropic/claude-sonnet-4-6",
    leaMaxTurns: 21
  }, state);

  assert.equal(openAiResult.statusCode, 200);
  assert.equal(openAiResult.body.leaProvider, "openai");
  assert.equal(openAiResult.body.leaModel, "gpt-5.4-mini");
  assert.equal(openAiResult.body.leaMaxTurns, 34);
  assert.equal(openAiResult.body.leaMaxSpendUsd, 9.5);
  assert.equal(openAiResult.body.leaTexMirrorEnabled, true);
  assert.equal(geminiResult.statusCode, 200);
  assert.equal(geminiResult.body.leaProvider, "google");
  assert.equal(geminiResult.body.leaModel, "gemini/gemini-2.5-flash");
  assert.equal(anthropicResult.statusCode, 200);
  assert.equal(anthropicResult.body.leaProvider, "anthropic");
  assert.equal(anthropicResult.body.leaModel, "anthropic/claude-sonnet-4-6");

  const envFile = await fs.readFile(state.envPath, "utf8");
  assert.match(envFile, /LEA_MODEL=anthropic\/claude-sonnet-4-6/);
  assert.match(envFile, /LEA_MAX_TURNS=21/);
  assert.match(envFile, /LEA_MAX_SPEND_USD=9.5/);

  const saved = JSON.parse(await fs.readFile(state.settingsPath, "utf8"));
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaModel"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaMaxTurns"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaMaxSpendUsd"), false);
  assert.equal(saved.leaTexMirrorEnabled, true);
});

test("settings persist the tex-mirror toggle off", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo, env: { OPENAI_API_KEY: "openai-key" } });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "o4-mini",
    leaMaxTurns: 20,
    leaTexMirrorEnabled: false
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.leaTexMirrorEnabled, false);
  const saved = JSON.parse(await fs.readFile(state.settingsPath, "utf8"));
  assert.equal(saved.leaTexMirrorEnabled, false);
});

function makeMirrorFetch(captured) {
  return async (url, options = {}) => {
    if (String(url).includes("/mirror")) {
      captured.push({ url: String(url), body: JSON.parse(options.body || "{}") });
      return { ok: true, status: 200, text: async () => JSON.stringify({ written: 2, changed: true, committed: false }) };
    }
    return { ok: true, status: 200, text: async () => "{}" };
  };
}

test("mirror-tex forwards the project's .tex set to the adapter by slug", async () => {
  const leaRepo = await makeLeaRepo();
  const captured = [];
  const state = await makeState({ leaRepoPath: leaRepo, fetchImpl: makeMirrorFetch(captured) });

  const res = await handleMirrorTex({
    overleafProjectId: "Project-1",
    files: [
      { path: "main.tex", content: "A" },
      { path: "", content: "dropped" },
      { path: "sections/intro.tex", content: "B" }
    ]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.equal(captured.length, 1);
  assert.match(captured[0].url, /\/api\/projects\/by-slug\/Project-1\/mirror$/);
  assert.equal(captured[0].body.source, "overleaf");
  assert.deepEqual(captured[0].body.files.map((file) => file.path), ["main.tex", "sections/intro.tex"]);
  assert.equal(res.body.mirroredFiles["main.tex"], createHash("sha256").update("A").digest("hex"));
  assert.equal(state.texMirrorSnapshots["Project-1"].mirrorRevision, res.body.mirrorRevision);
});

test("mirror-tex rejects a missing project id and respects the disable toggle", async () => {
  const leaRepo = await makeLeaRepo();

  const missing = await handleMirrorTex({ files: [] }, await makeState({ leaRepoPath: leaRepo }));
  assert.equal(missing.statusCode, 400);
  assert.equal(missing.body.error, "missing_project_id");

  const disabled = await handleMirrorTex(
    { overleafProjectId: "p1", files: [] },
    await makeState({ leaRepoPath: leaRepo, leaTexMirrorEnabled: false })
  );
  assert.equal(disabled.statusCode, 400);
  assert.equal(disabled.body.error, "tex_mirror_disabled");
});

test("formalize refuses a source version that the mirror did not acknowledge", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  state.texMirrorSnapshots = {
    "project-1": {
      files: {
        "main.tex": createHash("sha256").update("older source").digest("hex")
      },
      mirrorRevision: "old"
    }
  };

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "freshness_guard",
    targetText: "Every current source is current.",
    sourceFile: "main.tex",
    sourceFileHash: createHash("sha256").update("current source").digest("hex")
  }, state);

  assert.equal(result.statusCode, 409);
  assert.equal(result.body.error, "mirror_source_mismatch");
  assert.deepEqual(state.jobs, {});
});

test("formalize returns a structured error when the spend cap is already reached", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaMaxSpendUsd: 0,
    env: { OPENAI_API_KEY: "test-key" }
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "capped_theorem",
    targetText: "A theorem that cannot start."
  }, state);

  assert.equal(result.statusCode, 402);
  assert.equal(result.body.error, "max_spend_reached");
  assert.equal(result.body.message, "Max spend limit has been reached.");
  assert.deepEqual(state.jobs, {});
});

test("lean pane manifest returns missing-stub items without artifacts", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    activePath: "main.tex",
    files: [{
      path: "main.tex",
      content: [
        "\\documentclass{article}",
        "\\begin{theorem}\\label{thm:compactness}",
        "% lea: formalize label=compactness_criterion",
        "Every open cover has a finite subcover.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items.length, 1);
  assert.equal(res.body.items[0].label, "compactness_criterion");
  assert.equal(res.body.items[0].latexLabel, "thm:compactness");
  assert.equal(res.body.items[0].status, "missing-stub");
  assert.equal(res.body.items[0].leanDeclarationName, "compactness_criterion");
});

test("lean pane manifest preserves a terminal max-spend failure for item-level feedback", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  state.jobs.capped = {
    jobId: "capped",
    jobKey: "project-1:theorem:capped_theorem",
    status: "failed",
    finalStatus: "max_spend",
    error: "Max spend limit reached. Lea run was cancelled.",
    targetKind: "theorem",
    targetLabel: "capped_theorem",
    declarationName: "capped_theorem",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:capped}",
        "% lea: formalize label=capped_theorem",
        "A theorem whose run reached the cap.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "invalid");
  assert.equal(res.body.items[0].finalStatus, "max_spend");
  assert.equal(res.body.items[0].failureCode, "max_spend_reached");
  assert.equal(res.body.items[0].failureMessage, "Max spend limit reached. Lea run was cancelled.");
});

test("lean pane manifest surfaces sorry stubs and valid artifacts", async () => {
  const leaRepo = await makeLeaRepo();
  const stubContent = "theorem compactness_criterion : True := by\n  sorry\n";
  const definitionContent = "def locally_finite_family : Prop := True\n";
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: {
        compactness_criterion: ledgerEntry("compactness_criterion", {
          path: "compactness.lean",
          module_name: "Lea.Project1.compactness",
          has_sorry: true,
          content: stubContent
        }),
        locally_finite_family: ledgerEntry("locally_finite_family", {
          path: "locally_finite.lean",
          module_name: "Lea.Project1.locally_finite",
          kind: "definition",
          content: definitionContent
        })
      }
    })
  });
  await writeLeaProjectProof(
    leaRepo,
    path.join("workspace", "proofs", "Lea", "Project1", "compactness.lean"),
    stubContent
  );
  await writeLeaProjectProof(
    leaRepo,
    path.join("workspace", "proofs", "Lea", "Project1", "locally_finite.lean"),
    definitionContent
  );

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    activePath: "main.tex",
    files: [{
      path: "main.tex",
      content: [
        "\\documentclass{article}",
        "\\begin{theorem}\\label{thm:compactness}",
        "% lea: formalize label=compactness_criterion",
        "Every open cover has a finite subcover.",
        "\\end{theorem}",
        "\\begin{definition}\\label{def:locally-finite}",
        "% lea: define label=locally_finite_family",
        "A family is locally finite if every point has a neighborhood meeting finitely many members.",
        "\\end{definition}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  // The definition formalizes to `defined`, distinct from a proved theorem's `valid`.
  assert.deepEqual(res.body.items.map((item) => item.status), ["stub-generated", "defined"]);
  assert.match(res.body.items[0].leanStub, /theorem compactness_criterion/);
  assert.match(res.body.items[0].leanArtifactContent, /sorry/);
  assert.match(res.body.items[1].leanArtifactContent, /def locally_finite_family/);
  assert.equal(res.body.items[1].leanKind, "def");
});

test("lean pane manifest surfaces a disproof as a counterexample, not a failure", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const proofPath = path.join("workspace", "proofs", "false_claim.lean");
  await writeLeaProjectProof(leaRepo, proofPath, [
    "import Mathlib",
    "",
    "theorem false_claim_counterexample : True := by",
    "  trivial",
    ""
  ].join("\n"));
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "false_claim",
    proofPath
  });
  state.jobs.disproved = {
    jobId: "disproved",
    jobKey: "project-1:theorem:false_claim",
    status: "disproved",
    finalStatus: "disproved",
    resultKind: "disproved",
    targetKind: "theorem",
    targetLabel: "false_claim",
    declarationName: "false_claim",
    recordedProofPath: proofPath,
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:false}",
        "% lea: formalize label=false_claim",
        "Every group is abelian.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "disproved");
});

test("lean pane manifest surfaces needs_review metadata as unknown, not valid", async () => {
  // Reproduces the real bug: a job finished `needs_review` with no markdown
  // entry recorded (mirrors production, where markdown recording was skipped
  // entirely for this resultKind before the fix) and no other local evidence.
  // It should not keep a stale valid chip or invent a first-class needs-review
  // status without checked artifact evidence.
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  state.jobs.needsReview = {
    jobId: "needsReview",
    jobKey: "project-1:theorem:compactness_corollary",
    status: "needs_review",
    finalStatus: "needs_review",
    resultKind: "needs_review",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:corollary}",
        "% lea: formalize label=compactness_corollary",
        "A consequence that should follow from compactness_criterion.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "unknown");
});

// The terminal-outcome branches in getTheoremStatus used pairwise recency
// guards, so an older formalized job could shadow a newer unconfirmed re-run:
// the chip stayed "valid" even though the latest run produced no confirmed
// artifact. Now one selection picks the newest terminal job; these two tests pin
// both directions.
test("lean pane manifest: a newer unconfirmed re-run beats an older formalized job (regression)", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const base = {
    jobKey: "project-1:theorem:compactness_corollary",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173"
  };
  state.jobs.older = {
    ...base,
    jobId: "older",
    status: "formalized",
    finalStatus: "formalized",
    resultKind: "proved",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
  state.jobs.newer = {
    ...base,
    jobId: "newer",
    status: "needs_review",
    finalStatus: "needs_review",
    resultKind: "needs_review",
    startedAt: "2026-01-02T00:00:00.000Z",
    finishedAt: "2026-01-02T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:corollary}",
        "% lea: formalize label=compactness_corollary",
        "A consequence.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "unknown");
});

test("lean pane manifest: a newer formalized re-run beats an older needs_review job", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const base = {
    jobKey: "project-1:theorem:compactness_corollary",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173"
  };
  state.jobs.older = {
    ...base,
    jobId: "older",
    status: "needs_review",
    finalStatus: "needs_review",
    resultKind: "needs_review",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
  state.jobs.newer = {
    ...base,
    jobId: "newer",
    status: "formalized",
    finalStatus: "formalized",
    resultKind: "proved",
    startedAt: "2026-01-02T00:00:00.000Z",
    finishedAt: "2026-01-02T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:corollary}",
        "% lea: formalize label=compactness_corollary",
        "A consequence.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "valid");
});

// Regression for the live report: manually editing a proved theorem's proof
// to `sorry` left its pane chip reading "valid". `sorry` COMPILES (warning,
// not error), so no error verdict fires -- the demotion comes from the
// ledger's has_sorry evidence (the sorry-reinserted rule, re-homed into the
// adapter's answer by PLAN 4.4), which must beat the job's stale verdict.
test("lean pane manifest demotes a formalized item to stub-generated when the ledger sees a sorry in its file", async () => {
  const leaRepo = await makeLeaRepo();
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean");
  const targetStatus = {
    compactness_criterion: ledgerEntry("compactness_criterion", {
      has_sorry: true,
      content: "theorem compactness_criterion : True := by\n  sorry\n"
    })
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], { targetStatus })
  });
  state.jobs.a = {
    jobId: "a",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    finalStatus: "formalized",
    resultKind: "proved",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    recordedProofPath: proofPath,
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:criterion}",
      "% lea: formalize label=compactness_criterion",
      "Every open cover has a finite subcover.",
      "\\end{theorem}"
    ].join("\n")
  }];

  const withSorry = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(withSorry.statusCode, 200);
  assert.equal(withSorry.body.items[0].status, "stub-generated");

  // ...and once the ledger sees a real proof again, the chip returns to valid.
  targetStatus.compactness_criterion = ledgerEntry("compactness_criterion");
  const fixed = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(fixed.body.items[0].status, "valid");
});

// The other half of the same report: the DEPENDENT (whose import is now a
// sorry stub) showed the amber "!" on its document badge but a plain "valid"
// chip in the pane -- the doc overlay renders statusInfo.stubbedTheoremUses,
// while the pane enrichment silently dropped that field. The pane item now
// carries it too.
test("lean pane manifest surfaces stubbed-import uses on the dependent's pane item, matching the doc badge", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    // The stub's own chip demotion is ledger evidence (has_sorry); the
    // dependent's warning is file-derived (attachTransitiveStubbedUpstream).
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: {
        compactness_criterion: ledgerEntry("compactness_criterion", {
          has_sorry: true,
          content: "theorem compactness_criterion : True := by\n  sorry\n"
        })
      }
    })
  });
  const criterionPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean");
  const corollaryPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_corollary.lean");
  await writeLeaProjectProof(leaRepo, criterionPath, "theorem compactness_criterion : True := by\n  sorry\n");
  await writeLeaProjectProof(
    leaRepo,
    corollaryPath,
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  trivial\n"
  );
  state.jobs.criterion = {
    jobId: "criterion",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    recordedProofPath: criterionPath,
    moduleName: "Lea.Project1.compactness_criterion",
    targetTextHash: "",
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
  state.jobs.corollary = {
    jobId: "corollary",
    jobKey: "project-1:theorem:compactness_corollary",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    recordedProofPath: corollaryPath,
    moduleName: "Lea.Project1.compactness_corollary",
    targetUses: [{
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      moduleName: "Lea.Project1.compactness_criterion"
    }],
    targetTextHash: "",
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:criterion}",
      "% lea: formalize label=compactness_criterion",
      "Every open cover has a finite subcover.",
      "\\end{theorem}",
      "\\begin{theorem}\\label{thm:corollary}",
      "% lea: formalize label=compactness_corollary uses=compactness_criterion",
      "A consequence of compactness.",
      "\\end{theorem}"
    ].join("\n")
  }];

  const res = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(res.statusCode, 200);
  const byLabel = Object.fromEntries(res.body.items.map((item) => [item.leanDeclarationName, item]));

  // the stubbed item itself is demoted (previous test's fix)
  assert.equal(byLabel.compactness_criterion.status, "stub-generated");

  // the dependent still compiles -- "valid" is right -- but it now carries
  // the same stubbed-uses warning the doc badge renders as the amber "!"
  assert.equal(byLabel.compactness_corollary.status, "valid");
  assert.ok(Array.isArray(byLabel.compactness_corollary.stubbedTheoremUses));
  assert.equal(byLabel.compactness_corollary.stubbedTheoremUses[0].targetLabel, "compactness_criterion");
});

// Regression + feature for the live report ("nothing happens, not even the
// amber check on the directly reliant theorem"): the stubbed-uses warning
// used to be computed ONLY from job-recorded `targetUses` (formalize-time
// `uses=` links) -- so it silently vanished whenever jobs.json was cleared
// (start-dev.sh does this by default), and it never reached anything more
// than one hop from the stub. It is now ALSO derived from the files on disk,
// transitively: every formalized item whose import chain reaches a file that
// currently contains sorry/admit carries stubbedTheoremUses naming exactly
// what remains to be formalized upstream -- with NO job use-records needed.
test("stubbed-upstream warnings are file-derived and transitive: every downstream item lists what remains", async () => {
  const leaRepo = await makeLeaRepo();
  // Mutable ledger evidence for the stub itself: flipped to a clean entry
  // when the test rewrites the file below. The transitive warnings on the
  // dependents stay purely file-derived — no ledger entries for them.
  const targetStatus = {
    compactness_criterion: ledgerEntry("compactness_criterion", {
      has_sorry: true,
      content: "theorem compactness_criterion : True := by\n  sorry\n"
    })
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], { targetStatus })
  });
  const criterionPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean");
  const corollaryPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_corollary.lean");
  const applicationPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_application.lean");
  await writeLeaProjectProof(leaRepo, criterionPath, "theorem compactness_criterion : True := by\n  sorry\n");
  await writeLeaProjectProof(
    leaRepo,
    corollaryPath,
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  trivial\n"
  );
  await writeLeaProjectProof(
    leaRepo,
    applicationPath,
    // two hops from the stub -- imports only the corollary, never the stub itself
    "import Lea.Project1.compactness_corollary\ntheorem compactness_application : True := by\n  trivial\n"
  );
  const baseJob = (label, proofPath) => ({
    jobId: label,
    jobKey: `project-1:theorem:${label}`,
    status: "formalized",
    targetKind: "theorem",
    targetLabel: label,
    declarationName: label,
    recordedProofPath: proofPath,
    moduleName: `Lea.Project1.${label}`,
    // deliberately NO targetUses / stubbedTheoremUses: the warning must not
    // depend on formalize-time use-records having survived
    targetTextHash: "",
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  });
  state.jobs.criterion = baseJob("compactness_criterion", criterionPath);
  state.jobs.corollary = baseJob("compactness_corollary", corollaryPath);
  state.jobs.application = baseJob("compactness_application", applicationPath);

  const marker = (label, line) => [
    `\\begin{theorem}\\label{thm:${line}}`,
    `% lea: formalize label=${label}`,
    "Statement.",
    "\\end{theorem}"
  ].join("\n");
  const files = [{
    path: "main.tex",
    content: [
      marker("compactness_criterion", "criterion"),
      marker("compactness_corollary", "corollary"),
      marker("compactness_application", "application")
    ].join("\n")
  }];

  const res = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(res.statusCode, 200);
  const byLabel = Object.fromEntries(res.body.items.map((item) => [item.leanDeclarationName, item]));

  // the stub itself is demoted, and doesn't warn about itself
  assert.equal(byLabel.compactness_criterion.status, "stub-generated");
  assert.equal(byLabel.compactness_criterion.stubbedTheoremUses, undefined);

  // direct dependent: valid chip + upstream warning, from files alone
  assert.equal(byLabel.compactness_corollary.status, "valid");
  assert.deepEqual(
    (byLabel.compactness_corollary.stubbedTheoremUses || []).map((use) => use.targetLabel),
    ["compactness_criterion"]
  );

  // TRANSITIVE dependent (two hops): also warned, naming the real root cause
  assert.equal(byLabel.compactness_application.status, "valid");
  assert.deepEqual(
    (byLabel.compactness_application.stubbedTheoremUses || []).map((use) => use.targetLabel),
    ["compactness_criterion"]
  );

  // the doc-badge path (handleGetStatuses -> getTargetStatus) carries the
  // same warning, so the overlay's amber "!" agrees with the pane
  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "compactness_application", targetText: "Statement." }]
  }, state);
  const appStatus = statuses.body.statuses["theorem:compactness_application"];
  assert.equal(appStatus.status, "formalized");
  assert.equal(appStatus.hasStubbedTheoremUses, true);
  assert.deepEqual(appStatus.stubbedTheoremUses.map((use) => use.targetLabel), ["compactness_criterion"]);

  // ...and once the stub is really proven, every warning clears
  await writeLeaProjectProof(leaRepo, criterionPath, "theorem compactness_criterion : True := by\n  trivial\n");
  targetStatus.compactness_criterion = ledgerEntry("compactness_criterion");
  const fixed = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  const fixedByLabel = Object.fromEntries(fixed.body.items.map((item) => [item.leanDeclarationName, item]));
  assert.equal(fixedByLabel.compactness_criterion.status, "valid");
  assert.equal(fixedByLabel.compactness_corollary.stubbedTheoremUses, undefined);
  assert.equal(fixedByLabel.compactness_application.stubbedTheoremUses, undefined);
});

test("lean pane manifest marks generated artifacts stale when source hash changes", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const proofPath = path.join("workspace", "proofs", "Lea", "Project", "compactness.lean");
  await writeLeaProjectProof(leaRepo, proofPath, "theorem compactness_criterion : True := by\n  trivial\n");
  state.jobs.stale = {
    jobId: "stale",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    recordedProofPath: proofPath,
    targetTextHash: hashTargetText("The original source."),
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:compactness}",
        "% lea: formalize label=compactness_criterion",
        "The source has changed.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "stale");
  assert.equal(res.body.items[0].sourceFreshness, "stale");
  assert.equal(res.body.items[0].generatedFromSourceHash, hashTargetText("The original source."));

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      targetText: "The source has changed."
    }]
  }, state);
  const staleStatus = statuses.body.statuses["theorem:compactness_criterion"];
  assert.equal(staleStatus.status, "formalized");
  assert.equal(staleStatus.sourceFreshness, "stale");
  assert.equal(staleStatus.generatedFromSourceHash, hashTargetText("The original source."));

  const reverted = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      targetText: "The original source."
    }]
  }, state);
  assert.equal(reverted.body.statuses["theorem:compactness_criterion"].sourceFreshness, "current");
});

test("source freshness follows the restored artifact rather than a newer failed retry", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  state.jobs.original = {
    jobId: "original",
    jobKey: "project-1:theorem:restored_proof",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "restored_proof",
    declarationName: "restored_proof",
    targetTextHash: hashTargetText("Original statement."),
    leaRepoPath: leaRepo,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
  state.jobs.failed_retry = {
    jobId: "failed_retry",
    jobKey: "project-1:theorem:restored_proof",
    status: "failed",
    finalStatus: "failed",
    targetKind: "theorem",
    targetLabel: "restored_proof",
    declarationName: "restored_proof",
    targetTextHash: hashTargetText("Changed statement."),
    leaRepoPath: leaRepo,
    error: "Retry failed; previous artifact restored.",
    startedAt: "2026-01-02T00:00:00.000Z",
    finishedAt: "2026-01-02T00:01:00.000Z"
  };

  const result = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "restored_proof",
      targetText: "Changed statement."
    }]
  }, state);
  const info = result.body.statuses["theorem:restored_proof"];
  assert.equal(info.status, "failed");
  assert.equal(info.sourceFreshness, "stale");
  assert.equal(info.generatedFromSourceHash, hashTargetText("Original statement."));
});

test("line shifts outside a formalized block do not make either status surface stale", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const targetText = "Every open cover has a finite subcover.";
  state.jobs.positionIndependent = {
    jobId: "position-independent",
    jobKey: "project-1:theorem:position_independent",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "position_independent",
    declarationName: "position_independent",
    targetTextHash: hashTargetText(targetText),
    targetUses: [{
      targetKind: "theorem",
      targetLabel: "finite_subcover",
      declarationName: "finite_subcover"
    }],
    targetContext: "Apply compactness first.",
    // Simulate a fingerprint produced by an older implementation that mixed
    // location data into the opaque hash. Structured block inputs are the
    // authoritative fallback and prove that the block itself is unchanged.
    formalizationInputHash: "legacy-position-dependent-fingerprint",
    leaRepoPath: leaRepo,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const status = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "position_independent",
      targetText,
      targetUses: ["finite_subcover"],
      targetContext: "Apply compactness first.",
      sourceStartLine: 200,
      sourceEndLine: 204
    }]
  }, state);
  assert.equal(
    status.body.statuses["theorem:position_independent"].sourceFreshness,
    "current"
  );

  const pane = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "Unrelated text above the theorem.",
        "",
        "",
        "\\begin{theorem}",
        "% lea: formalize label=position_independent uses={finite_subcover} context={Apply compactness first.}",
        targetText,
        "\\end{theorem}",
        "",
        "",
        "Unrelated text below the theorem."
      ].join("\n")
    }]
  }, state);
  assert.equal(pane.body.items[0].status, "valid");
  assert.equal(pane.body.items[0].sourceFreshness, "current");
});

test("changes to uses and context mark both status surfaces stale without changing theorem text", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const targetText = "Every open cover has a finite subcover.";
  state.jobs.metadata = {
    jobId: "metadata",
    jobKey: "project-1:theorem:metadata_sensitive",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "metadata_sensitive",
    declarationName: "metadata_sensitive",
    targetTextHash: hashTargetText(targetText),
    formalizationInputHash: hashFormalizationInput({
      targetKind: "theorem",
      targetText,
      targetUses: [],
      targetContext: ""
    }),
    leaRepoPath: leaRepo,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
  state.jobs.legacyMetadata = {
    ...state.jobs.metadata,
    jobId: "legacy-metadata",
    jobKey: "project-1:theorem:legacy_metadata_sensitive",
    targetLabel: "legacy_metadata_sensitive",
    declarationName: "legacy_metadata_sensitive",
    formalizationInputHash: undefined,
    targetUses: [],
    targetContext: ""
  };

  const changedUses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "metadata_sensitive",
      targetText,
      targetUses: ["finite_subcover"],
      targetContext: ""
    }]
  }, state);
  assert.equal(changedUses.body.statuses["theorem:metadata_sensitive"].sourceFreshness, "stale");

  const changedContext = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "metadata_sensitive",
      targetText,
      targetUses: [],
      targetContext: "Apply compactness first."
    }]
  }, state);
  assert.equal(changedContext.body.statuses["theorem:metadata_sensitive"].sourceFreshness, "stale");

  const unchanged = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "metadata_sensitive",
      targetText,
      targetUses: [],
      targetContext: ""
    }]
  }, state);
  assert.equal(unchanged.body.statuses["theorem:metadata_sensitive"].sourceFreshness, "current");

  const legacyChangedUses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{
      targetKind: "theorem",
      targetLabel: "legacy_metadata_sensitive",
      targetText,
      targetUses: ["finite_subcover"],
      targetContext: ""
    }]
  }, state);
  assert.equal(
    legacyChangedUses.body.statuses["theorem:legacy_metadata_sensitive"].sourceFreshness,
    "stale"
  );
  assert.ok(
    legacyChangedUses.body.statuses["theorem:legacy_metadata_sensitive"].generatedFromInputHash,
    "pre-upgrade jobs should derive a composite freshness hash from stored metadata"
  );

  const pane = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}",
        "% lea: formalize label=metadata_sensitive uses={finite_subcover}",
        targetText,
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);
  assert.equal(pane.body.items[0].status, "stale");
  assert.equal(pane.body.items[0].sourceFreshness, "stale");
});

test("approval revisions agree across status surfaces and change with transitive upstream artifacts", async () => {
  const leaRepo = await makeLeaRepo();
  const basePath = path.join("workspace", "proofs", "Lea", "Project1", "base.lean");
  const middlePath = path.join("workspace", "proofs", "Lea", "Project1", "middle.lean");
  const resultPath = path.join("workspace", "proofs", "Lea", "Project1", "result.lean");
  await writeLeaProjectProof(leaRepo, basePath, "theorem base : True := by\n  trivial\n");
  await writeLeaProjectProof(
    leaRepo,
    middlePath,
    "import Lea.Project1.base\n\ntheorem middle : True := by\n  exact base\n"
  );
  await writeLeaProjectProof(
    leaRepo,
    resultPath,
    "import Lea.Project1.middle\n\ntheorem result : True := by\n  exact middle\n"
  );
  const targetStatus = {
    result: ledgerEntry("result", {
      content: "import Lea.Project1.middle\n\ntheorem result : True := by\n  exact middle\n"
    })
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], { targetStatus })
  });
  const target = {
    targetKind: "theorem",
    targetLabel: "result",
    targetText: "The result follows from the middle theorem.",
    targetUses: ["middle"],
    targetContext: ""
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [target]
  }, state);
  const statusInfo = statuses.body.statuses["theorem:result"];
  assert.equal(statusInfo.approvalEligible, true);
  assert.match(statusInfo.approvalRevision, /^[a-f0-9]{64}$/);

  const pane = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}",
        "% lea: formalize label=result uses={middle}",
        target.targetText,
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);
  assert.equal(pane.body.items[0].approvalEligible, true);
  assert.equal(pane.body.items[0].approvalRevision, statusInfo.approvalRevision);

  await writeLeaProjectProof(
    leaRepo,
    basePath,
    "theorem base : True := by\n  exact True.intro\n"
  );
  const changed = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [target]
  }, state);
  assert.notEqual(
    changed.body.statuses["theorem:result"].approvalRevision,
    statusInfo.approvalRevision
  );
});

test("lean pane manifest uses adapter session code for formalized jobs without recorded proof paths", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeAdapterApiFetch(calls, {
      sessionId: "sess-formalized",
      sessionDetail: {
        project_namespace: "Lea.Project",
        runs: [{ id: "api-run-1", input_tokens: 0, output_tokens: 0, cost_usd: 0 }],
        code_steps: [{
          id: "step-1",
          seq: 1,
          path: "Compactness.lean",
          code: "@[simp]\ntheorem compactness_criterion : True := by\n  trivial\n",
          check_status: "ok"
        }]
      }
    })
  });
  state.jobs.formalized = {
    jobId: "formalized",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    targetTextHash: "",
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaUiBaseUrl: "http://localhost:5173",
    leaSessionId: "sess-formalized",
    apiRunId: "api-run-1",
    projectSlug: "project-1",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:compactness}",
        "% lea: formalize label=compactness_criterion",
        "Every open cover has a finite subcover.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "valid");
  assert.match(res.body.items[0].leanStub, /theorem compactness_criterion : True/);
  assert.match(res.body.items[0].leanArtifactContent, /trivial/);
  assert.match(res.body.items[0].leanArtifactPath, /Compactness\.lean/);
  assert.ok(calls.some((call) => String(call.url).includes("/api/sessions/sess-formalized")));
});

test("lean pane manifest degrades when Lea lookup is unavailable", async () => {
  const state = await makeState();

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:main}",
        "% lea: formalize label=main_theorem",
        "A.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "unknown");
  assert.equal(res.body.diagnostics.at(-1).code, "lea_unconfigured");
});

test("records targetSyntax on the job for telemetry, defaulting to comment", async () => {
  const leaRepo = await makeLeaRepo();

  const defaultState = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });
  const defaultResult = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "syntax_default_test",
    targetText: "A theorem."
    // no syntax field
  }, defaultState);
  assert.equal(defaultState.jobs[defaultResult.body.jobId].targetSyntax, "comment");
  assert.deepEqual(defaultState.jobs[defaultResult.body.jobId].formalizationSourceUses, []);
  assert.equal(
    defaultState.jobs[defaultResult.body.jobId].formalizationInputHash,
    hashFormalizationInput({
      targetKind: "theorem",
      targetText: "A theorem.",
      targetUses: [],
      targetContext: ""
    })
  );
  // Snapshot NOW, at the same just-created lifecycle point the tag job will
  // be snapshotted at -- the run continues in the background and starts
  // mutating the job (leaSessionId, apiRunId, ...) while the second
  // state/formalize is being set up, so a deferred snapshot races against
  // run progress.
  const defaultJob = { ...defaultState.jobs[defaultResult.body.jobId] };

  const tagState = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });
  const tagResult = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "syntax_tag_test",
    targetText: "A theorem.",
    syntax: "tag"
  }, tagState);
  assert.equal(tagState.jobs[tagResult.body.jobId].targetSyntax, "tag");
  const tagJob = { ...tagState.jobs[tagResult.body.jobId] };

  // Identity (jobKey) and every field that reaches the prompt are unaffected
  // by syntax -- a tag-sourced and comment-sourced target with the same
  // targetKind/targetLabel/targetText/targetUses/targetContext produce
  // identical jobs apart from this one telemetry field and the inputs that
  // differ by construction (jobId/timestamps/label/jobKey/hash).
  for (const key of ["jobId", "jobKey", "targetLabel", "targetSyntax", "targetTextHash", "startedAt", "logPath", "absolutePath", "relativePath", "declarationName"]) {
    delete defaultJob[key];
    delete tagJob[key];
  }
  assert.deepEqual(defaultJob, tagJob);
});

test("lean pane manifest flags in-progress items for live polling", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });

  const started = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    targetText: "Every open cover has a finite subcover."
  }, state);
  state.jobs[started.body.jobId].leaCurrentTurn = 7;
  state.jobs[started.body.jobId].leaMaxTurns = 20;

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:compactness}",
        "% lea: formalize label=compactness_criterion",
        "Every open cover has a finite subcover.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "in-progress");
  assert.equal(res.body.items[0].inProgress, true);
  assert.deepEqual(res.body.items[0].turnProgress, { current: 7, max: 20 });
});

test("lean pane manifest keeps polling when an active job already has valid proof evidence", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" }
  });
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "valid_before_valid_pane.lean");
  await writeLeaProjectProof(leaRepo, proofPath, "theorem valid_before_valid_pane : True := by\n  trivial\n");
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "valid_before_valid_pane",
    proofPath,
    moduleName: "Lea.Project1.valid_before_valid_pane"
  });
  state.jobs.active_with_valid_file = {
    jobId: "active_with_valid_file",
    jobKey: "project-1:theorem:valid_before_valid_pane",
    status: "in_progress",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "valid_before_valid_pane",
    declarationName: "valid_before_valid_pane",
    leaSessionId: "sess-still-running",
    leaUiBaseUrl: "http://localhost:5173",
    leaRepoPath: leaRepo,
    recordedProofPath: proofPath,
    moduleName: "Lea.Project1.valid_before_valid_pane",
    startedAt: "2026-01-01T00:00:00.000Z"
  };

  const res = await handleLeanPaneManifest({
    overleafProjectId: "project-1",
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:valid-before-valid}",
        "% lea: formalize label=valid_before_valid_pane",
        "A theorem.",
        "\\end{theorem}"
      ].join("\n")
    }]
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.items[0].status, "in-progress");
  assert.equal(res.body.items[0].inProgress, true);
});

test("settings clear max spend and reject negative caps", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaMaxSpendUsd: 4,
    env: { OPENAI_API_KEY: "openai-key" }
  });

  const cleared = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "o4-mini",
    leaMaxTurns: 20,
    leaMaxSpendUsd: null
  }, state);
  const negative = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "o4-mini",
    leaMaxTurns: 20,
    leaMaxSpendUsd: -1
  }, state);

  assert.equal(cleared.statusCode, 200);
  assert.equal(cleared.body.leaMaxSpendUsd, null);
  assert.equal(negative.statusCode, 400);
  assert.equal(negative.body.error, "invalid_max_spend");
});

test("settings normalize legacy Anthropic model ids", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaModel: "anthropic/claude-sonnet-4-20250514",
    env: { ANTHROPIC_API_KEY: "anthropic-key" }
  });

  assert.equal((await buildSettingsResponse(state)).leaModel, "anthropic/claude-sonnet-4-6");

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "anthropic/claude-opus-4-20250514",
    leaMaxTurns: 20
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.leaModel, "anthropic/claude-opus-4-8");
});

test("settings writes scrub legacy key fields", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaModel: "anthropic/claude-sonnet-4-20250514",
    leaApiKey: "legacy-openai-key",
    leaProviderApiKeys: { anthropic: "legacy-anthropic-key" },
    env: {}
  });

  const startup = await ensureStartupLeaRuntime(state);
  assert.equal(startup.ok, true);
  assert.equal(state.settings.leaModel, "anthropic/claude-sonnet-4-6");
  assert.equal(state.settings.leaApiKey, undefined);
  assert.equal(state.settings.leaProviderApiKeys, undefined);
  assert.equal(state.env.ANTHROPIC_API_KEY, "legacy-anthropic-key");
  assert.equal(state.env.OPENAI_API_KEY, "legacy-openai-key");

  const envFile = await fs.readFile(state.envPath, "utf8");
  assert.match(envFile, /ANTHROPIC_API_KEY=legacy-anthropic-key/);
  assert.match(envFile, /OPENAI_API_KEY=legacy-openai-key/);
  const saved = JSON.parse(await fs.readFile(state.settingsPath, "utf8"));
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaModel"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaApiKey"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaProviderApiKeys"), false);
});

test("settings save provider key patches to env file without settings persistence", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "openai-key" }
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "anthropic/claude-sonnet-4-6",
    leaMaxTurns: 20,
    leaProviderApiKeys: { anthropic: "anthropic-key" }
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.leaProviderKeys.anthropic.configured, true);
  assert.equal(state.env.ANTHROPIC_API_KEY, "anthropic-key");

  const envFile = await fs.readFile(state.envPath, "utf8");
  assert.match(envFile, /ANTHROPIC_API_KEY=anthropic-key/);
  assert.match(envFile, /LEA_MODEL=anthropic\/claude-sonnet-4-6/);
  assert.match(envFile, /LEA_MAX_TURNS=20/);

  const saved = JSON.parse(await fs.readFile(state.settingsPath, "utf8"));
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaApiKey"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaProviderApiKeys"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaModel"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(saved, "leaMaxTurns"), false);
});

test("adapter settings read overlays shared values and key status", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapter = makeAdapterStore({
    model: "claude-sonnet-4-6",
    max_turns: 55,
    max_spend_usd: 7.5,
    api_keys: { ANTHROPIC_API_KEY: keyStatus("dcba") }
  });
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaModel: "o4-mini",
    env: { OPENAI_API_KEY: "env-openai" },
    fetchImpl: makeAdapterFetch(adapter, calls)
  });

  const response = await buildSettingsResponse(state);

  // The adapter (single source of truth) wins for the shared scalars...
  assert.equal(response.leaModel, "claude-sonnet-4-6");
  assert.equal(response.leaMaxTurns, 55);
  assert.equal(response.leaMaxSpendUsd, 7.5);
  // ...and a key configured only in the adapter (lea-standalone UI) reads as configured.
  assert.equal(response.leaProviderKeys.anthropic.configured, true);
  // while a key in the companion env is still recognized.
  assert.equal(response.leaProviderKeys.openai.configured, true);
  assert.ok(calls.some((call) => call.method === "GET" && call.url.endsWith("/api/settings")));
});

test("saving settings forwards shared fields to the adapter", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapter = makeAdapterStore({ api_keys: { OPENAI_API_KEY: keyStatus("akey") } });
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "env-openai" },
    fetchImpl: makeAdapterFetch(adapter, calls)
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gpt-5.4-mini",
    leaMaxTurns: 42,
    leaMaxSpendUsd: 3.5
  }, state);

  assert.equal(result.statusCode, 200);
  const put = calls.find((call) => call.method === "PUT");
  assert.ok(put, "expected a PUT to /api/settings");
  assert.equal(put.body.model, "gpt-5.4-mini");
  assert.equal(put.body.max_turns, 42);
  assert.equal(put.body.max_spend_usd, 3.5);
  // the selected model's key is forwarded so the adapter holds what the model needs
  assert.equal(put.body.api_keys.OPENAI_API_KEY.value, "env-openai");
  // and the adapter store actually moved
  assert.equal(adapter.settings.model, "gpt-5.4-mini");
  assert.equal(adapter.settings.max_turns, 42);
});

test("adapter validation rejection surfaces as a 422", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapter = makeAdapterStore({
    api_keys: { ANTHROPIC_API_KEY: keyStatus("good") },
    reject: { field: "api_keys.ANTHROPIC_API_KEY", message: "The Anthropic API key was rejected by the provider." }
  });
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: {},
    fetchImpl: makeAdapterFetch(adapter, calls)
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "anthropic/claude-sonnet-4-6",
    leaMaxTurns: 20
  }, state);

  assert.equal(result.statusCode, 422);
  assert.equal(result.body.error, "adapter_settings_rejected");
  assert.equal(result.body.field, "api_keys.ANTHROPIC_API_KEY");
  assert.match(result.body.message, /rejected by the provider/);
});

test("settings reject invalid submitted Gemini keys before persistence", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "openai-key" },
    fetchImpl: makeProviderValidationFetch(calls, { google: 401 })
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gemini/gemini-2.5-pro",
    leaMaxTurns: 20,
    leaProviderApiKeys: { gemini: "PLACEHOLDER" }
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "invalid_google_key");
  assert.match(result.body.message, /Google AI API key was rejected/);
  assert.equal(state.env.GEMINI_API_KEY, undefined);
  assert.equal(await fileExists(state.envPath), false);
  assert.deepEqual(calls.map((call) => call.family), ["google"]);
});

test("settings reject invalid existing key for selected provider family", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { GEMINI_API_KEY: "bad-gemini-key" },
    fetchImpl: makeProviderValidationFetch(calls, { google: 403 })
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gemini/gemini-2.5-flash",
    leaMaxTurns: 20
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "invalid_google_key");
  assert.equal(state.settings.leaModel, "o4-mini");
  assert.deepEqual(calls.map((call) => call.family), ["google"]);
});

test("settings validate newly entered non-selected provider keys", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "openai-key" },
    fetchImpl: makeProviderValidationFetch(calls, { anthropic: 401 })
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gpt-5.4-mini",
    leaMaxTurns: 20,
    leaProviderApiKeys: { anthropic: "bad-anthropic-key" }
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "invalid_anthropic_key");
  assert.equal(state.env.ANTHROPIC_API_KEY, undefined);
  assert.deepEqual(calls.map((call) => call.family), ["anthropic"]);
});

test("settings save valid submitted keys after provider verification", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "openai-key" },
    fetchImpl: makeProviderValidationFetch(calls)
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gemini/gemini-2.5-flash",
    leaMaxTurns: 20,
    leaProviderApiKeys: { gemini: "valid-gemini-key" }
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.leaProviderKeys.google.configured, true);
  assert.equal(state.env.GEMINI_API_KEY, "valid-gemini-key");
  assert.deepEqual(calls.map((call) => call.family), ["google"]);

  const envFile = await fs.readFile(state.envPath, "utf8");
  assert.match(envFile, /GEMINI_API_KEY=valid-gemini-key/);
});

test("settings reject provider key verification network failures", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "openai-key" },
    fetchImpl: makeProviderValidationFetch(calls, { openai: "throw" })
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gpt-5.4-mini",
    leaMaxTurns: 20
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "openai_key_verification_failed");
  assert.match(result.body.message, /Could not verify OpenAI API key/);
});

test("settings do not validate untouched non-selected provider keys", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: {
      OPENAI_API_KEY: "openai-key",
      GEMINI_API_KEY: "existing-gemini-key",
      ANTHROPIC_API_KEY: "existing-anthropic-key"
    },
    fetchImpl: makeProviderValidationFetch(calls)
  });

  const result = await handleUpdateLeaSettings({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: "gpt-5.4",
    leaMaxTurns: 20
  }, state);

  assert.equal(result.statusCode, 200);
  assert.deepEqual(calls.map((call) => call.family), ["openai"]);
});

test("completed formalization status keeps Lea UI session link", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: { linked_done_test: ledgerEntry("linked_done_test") }
    })
  });
  // The job carries only the session link (and a pre-vocabulary "success"
  // status the overlay's terminal filters ignore); the formalized verdict
  // comes from the ledger evidence.
  state.jobs.completed_link = {
    jobId: "completed_link",
    jobKey: "project-1:theorem:linked_done_test",
    status: "success",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "linked_done_test",
    declarationName: "linked_done_test",
    recorderSessionId: "sess-done",
    recorderRunId: "run-done",
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "linked_done_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:linked_done_test"].status, "formalized");
  assert.equal(statuses.body.statuses["theorem:linked_done_test"].leaSessionId, "sess-done");
  assert.equal(statuses.body.statuses["theorem:linked_done_test"].leaSessionUrl, "http://localhost:5173/?session=sess-done");
});

test("in-progress status links Lea UI session from leaSessionId (no recorder)", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" }
  });
  state.jobs.active_link = {
    jobId: "active_link",
    jobKey: "project-1:theorem:in_progress_test",
    status: "in_progress",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "in_progress_test",
    declarationName: "in_progress_test",
    // Adapter session id from run start; recorder CLI never ran.
    leaSessionId: "sess-running",
    recorderSessionId: null,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z"
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "in_progress_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:in_progress_test"].status, "in_progress");
  assert.equal(statuses.body.statuses["theorem:in_progress_test"].leaSessionId, "sess-running");
  assert.equal(statuses.body.statuses["theorem:in_progress_test"].leaSessionUrl, "http://localhost:5173/?session=sess-running");
});

test("active jobs stay in progress even when local proof evidence already looks formalized", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" }
  });
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "valid_before_valid.lean");
  await writeLeaProjectProof(leaRepo, proofPath, "theorem valid_before_valid : True := by\n  trivial\n");
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "valid_before_valid",
    proofPath,
    moduleName: "Lea.Project1.valid_before_valid"
  });
  state.jobs.active_with_valid_file = {
    jobId: "active_with_valid_file",
    jobKey: "project-1:theorem:valid_before_valid",
    status: "in_progress",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "valid_before_valid",
    declarationName: "valid_before_valid",
    leaSessionId: "sess-still-running",
    leaUiBaseUrl: "http://localhost:5173",
    leaRepoPath: leaRepo,
    recordedProofPath: proofPath,
    moduleName: "Lea.Project1.valid_before_valid",
    startedAt: "2026-01-01T00:00:00.000Z"
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "valid_before_valid", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  const status = statuses.body.statuses["theorem:valid_before_valid"];
  assert.equal(status.status, "in_progress");
  assert.equal(status.leaSessionId, "sess-still-running");
  assert.equal(status.leaSessionUrl, "http://localhost:5173/?session=sess-still-running");
});

test("a newer repaired job is terminal evidence: its declarationName beats the older formalize job's stale cache", async () => {
  // Live bug: rename bookkeeping writes the item's current declarationName to
  // its job records, but status derivation's terminal-candidate list didn't
  // include `repaired` jobs at all. With no file evidence (no markdown entry,
  // no conventional proof file), status fell back to the OLDER formalize job,
  // whose stale declarationName then steered the pane's session-artifact
  // lookup onto the newest pre-rename snapshot -- the item displayed code it
  // had before the rename, while the editor (which reads the newest
  // session-linked job) showed the renamed file.
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" }
  });
  const base = {
    jobKey: "project-1:theorem:renamed_after_repair",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "renamed_after_repair",
    leaSessionId: "sess-repair-rename",
    leaUiBaseUrl: "http://localhost:5173"
  };
  state.jobs.formalize_run = {
    ...base,
    jobId: "formalize_run",
    status: "formalized",
    declarationName: "renamed_after_repair", // stale: never refreshed by the rename
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };
  state.jobs.repair_run = {
    ...base,
    jobId: "repair_run",
    status: "repaired",
    mode: "repair",
    declarationName: "renamed_after_repair_v2", // fresh: the rename landed here
    lastEditCheckStatus: "ok",
    startedAt: "2026-01-02T00:00:00.000Z",
    finishedAt: "2026-01-02T00:00:01.000Z"
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "renamed_after_repair", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  const status = statuses.body.statuses["theorem:renamed_after_repair"];
  // a verified repair reads as formalized, from the repair job itself
  assert.equal(status.status, "formalized");
  assert.equal(status.jobId, "repair_run");
  assert.equal(status.declarationName, "renamed_after_repair_v2");
});

test("statuses report saved sorry stubs", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: {
        saved_stub_test: ledgerEntry("saved_stub_test", {
          path: "SavedStub.lean",
          module_name: "Lea.Project1.SavedStub",
          has_sorry: true,
          content: "theorem saved_stub_test : True := by\n  sorry\n"
        })
      }
    })
  });

  const result = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "saved_stub_test", targetText: "A theorem." }]
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.statuses["theorem:saved_stub_test"].status, "sorry_stub");
  assert.equal(result.body.statuses["theorem:saved_stub_test"].leanStatement, "theorem saved_stub_test : True");
});

test("statuses report active Lea jobs as in progress", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });

  await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "active_status_test",
    targetText: "A theorem."
  }, state);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "active_status_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:active_status_test"].status, "in_progress");
});

test("statuses include turn progress for active Lea jobs", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([], {
      eventFrames: [
        { type: "turn_started", turn: 6 }
      ]
    })
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "active_progress_test",
    targetText: "A theorem."
  }, state);

  await waitFor(() => state.jobs[result.body.jobId]?.leaCurrentTurn === 6);
  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "active_progress_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(state.jobs[result.body.jobId].leaCurrentTurn, 6);
  assert.equal(state.jobs[result.body.jobId].leaMaxTurns, 20);
});

test("active Lea job omits turn progress when current turn is unknown or invalid", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([], {
      eventFrames: [
        { type: "agent_progress", current_turn: 0, max_turns: 20 }
      ]
    })
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "unknown_turn_test",
    targetText: "A theorem."
  }, state);

  await waitFor(() => state.jobs[result.body.jobId]?.status === "in_progress");
  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "unknown_turn_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:unknown_turn_test"].status, "in_progress");
  assert.equal(statuses.body.statuses["theorem:unknown_turn_test"].turnProgress, undefined);
});

test("statuses report formalized proofs recorded in the adapter ledger", async () => {
  const leaRepo = await makeLeaRepo();
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "ProjectProof.lean");
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: {
        project_markdown_test: ledgerEntry("project_markdown_test", {
          path: "ProjectProof.lean",
          module_name: "Lea.Project1.ProjectProof",
          content: "namespace Lea.Project1\n\nlemma project_markdown_test : True := by\n  trivial\n\nend Lea.Project1\n"
        })
      }
    })
  });

  const configured = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "project_markdown_test", targetText: "A theorem." }]
  }, state);
  const status = configured.body.statuses["theorem:project_markdown_test"];
  assert.equal(configured.statusCode, 200);
  assert.equal(status.status, "formalized");
  assert.equal(status.leanStatement, "lemma project_markdown_test : True");
  assert.equal(status.projectId, "project-1");
  assert.equal(status.projectSlug, "project-1");
  assert.equal(status.projectMarkdownPath, path.join(leaRepo, "workspace", "projects", "project-1.md"));
  assert.equal(status.recordedProofPath, proofPath);
  assert.equal(status.moduleName, "Lea.Project1.ProjectProof");
  assert.equal(status.absolutePath, path.join(leaRepo, proofPath));
});

test("formalize maps an Overleaf label to the Lean artifact Lea records", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "even_square_of_even.lean");
  const restorePath = await installFakeLake();
  try {
    // The adapter's run finalizer records the artifact row + ledger evidence
    // mid-run (the only identification source since the markdown-diff
    // fallback was deleted, 4.3); the onStatusRequest hook plays that role.
    const artifacts = [];
    const targetStatus = {};
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        targetStatus,
        onStatusRequest: async () => {
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "theorem even_square_of_even : True := by\n  trivial\n"
          );
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "even_square_of_even",
            path: "even_square_of_even.lean",
            module_name: "Lea.Project1.even_square_of_even",
            kind: "proof"
          });
          targetStatus.even_square_of_even = ledgerEntry("even_square_of_even");
        }
      })
    });
    const sourceContent = [
      "\\documentclass{article}",
      "\\begin{document}",
      "Nearby prose defines the parity notation.",
      "\\begin{theorem} Every doubled even number has an even square. \\end{theorem}",
      "\\end{document}"
    ].join("\n");
    const sourceFileHash = createHash("sha256").update(sourceContent).digest("hex");
    state.texMirrorSnapshots = {
      "project-1": {
        files: { "chapters/parity notes.tex": sourceFileHash },
        mirrorRevision: "mirror-revision"
      }
    };

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "epsilon_one",
      targetText: [
        "Formalize this theorem as part of project epsilon.",
        "Theorem name: even_square_of_even",
        "Lean signature:",
        "theorem even_square_of_even : True := by"
      ].join("\n"),
      sourceFile: "chapters/parity notes.tex",
      sourceStartLine: 4,
      sourceEndLine: 4,
      sourceFileHash,
      sourceExcerpt: sourceContent,
      sourceExcerptStartLine: 1,
      sourceExcerptEndLine: 5,
      sourceCorpusFileCount: 2,
      sourceCorpusChars: 1200
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.match(calls[0].body.task, /Overleaf theorem labeled epsilon_one/);
    assert.match(calls[0].body.task, /use that name/);
    assert.match(calls[0].body.task, /chapters\/parity notes\.tex, lines 4-4/);
    assert.match(calls[0].body.task, /\.lea\/files\/overleaf\/chapters\/parity-notes\.tex/);
    assert.match(calls[0].body.task, /read all mirrored LaTeX source files before planning/i);
    assert.match(calls[0].body.task, /Nearby prose defines the parity notation/);
    assert.match(calls[0].body.task, /no sorry\/admit in theorem even_square_of_even/);
    const job = state.jobs[result.body.jobId];
    assert.equal(job.targetLabel, "epsilon_one");
    assert.equal(job.declarationName, "even_square_of_even");
    assert.equal(job.recordedProofPath, proofPath);
    assert.equal(job.moduleName, "Lea.Project1.even_square_of_even");
    assert.equal(job.sourceContext.sourceFileHash, sourceFileHash);
    assert.equal(job.sourceContext.contextAcquisitionMode, "full-corpus");

    const statuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "theorem", targetLabel: "epsilon_one", targetText: "A theorem." }]
    }, state);

    const status = statuses.body.statuses["theorem:epsilon_one"];
    assert.equal(statuses.statusCode, 200);
    assert.equal(status.status, "formalized");
    assert.equal(status.targetLabel, "epsilon_one");
    assert.equal(status.declarationName, "even_square_of_even");
    assert.equal(status.recordedProofPath, proofPath);
    assert.equal(status.leanStatement, "theorem even_square_of_even : True");
  } finally {
    restorePath();
  }
});

// Root of the "manual sorry edit never demoted the chip" report: a verified
// run where the agent never self-registered a project-markdown entry recorded
// NO recordedProofPath/moduleName on the job and NO markdown entry -- leaving
// the target with a terminal verdict and no file link at all. The finalizer
// must backfill both from the adapter's artifact index (which recorded the
// run's FileChanged regardless of whether the agent self-reported).
test("a verified run that never self-registered still records proof path, module, and markdown entry", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean");
  const restorePath = await installFakeLake();
  try {
    const artifacts = [];
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        onStatusRequest: async () => {
          // the proof file lands on disk and the adapter finalizer indexes
          // it, but the agent writes NO markdown entry
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "theorem compactness_criterion : True := by\n  trivial\n"
          );
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "compactness_criterion",
            path: "compactness_criterion.lean",
            module_name: "Lea.Project1.compactness_criterion",
            kind: "proof"
          });
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      targetText: "Every open cover has a finite subcover."
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    const job = state.jobs[result.body.jobId];
    assert.equal(job.recordedProofPath, proofPath);
    assert.equal(job.moduleName, "Lea.Project1.compactness_criterion");
    const markdown = await fs.readFile(path.join(leaRepo, "workspace", "projects", "project-1.md"), "utf8");
    assert.match(markdown, /lea:theorem name="compactness_criterion"/);
  } finally {
    restorePath();
  }
});

// Same unregistered-artifact gap, but the agent picked a file name that does
// not match the Overleaf label -- the conventional-path probe can't see it, so
// the recovery must locate the file via the session's code_steps (previously
// this recovery ran only for needs_review exits, never for verified runs).
test("a verified unregistered run whose file name differs from the label is recovered via the session's code_steps", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "even_square_of_even.lean");
  const code = "theorem even_square_of_even : True := by\n  trivial\n";
  const restorePath = await installFakeLake();
  try {
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        sessionBody: {
          project_namespace: "Lea.Project1",
          code_steps: [{ path: "even_square_of_even.lean", seq: 1, code }]
        },
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, proofPath, code);
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "epsilon_one",
      targetText: [
        "Theorem name: even_square_of_even",
        "Lean signature:",
        "theorem even_square_of_even : True := by"
      ].join("\n")
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    const job = state.jobs[result.body.jobId];
    assert.equal(job.declarationName, "even_square_of_even");
    assert.equal(job.recordedProofPath, proofPath);
    assert.equal(job.moduleName, "Lea.Project1.even_square_of_even");
    const markdown = await fs.readFile(path.join(leaRepo, "workspace", "projects", "project-1.md"), "utf8");
    assert.match(markdown, /lea:theorem name="even_square_of_even"/);
  } finally {
    restorePath();
  }
});

test("formalize includes resolved theorem uses in the Lea prompt", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const dependencyProofPath = path.join("workspace", "proofs", "Lea", "Project1", "even_square_of_even.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "even_square_of_double_plus_double.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(
      leaRepo,
      dependencyProofPath,
      "theorem even_square_of_even : True := by\n  trivial\n"
    );
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        onStatusRequest: async () => {
          await writeLeaProjectProof(
            leaRepo,
            targetProofPath,
            "theorem even_square_of_double_plus_double : True := by\n  trivial\n"
          );
          await writeLeaProjectMarkdown(leaRepo, "project-1", {
            theoremName: "even_square_of_double_plus_double",
            proofPath: targetProofPath,
            moduleName: "Lea.Project1.even_square_of_double_plus_double"
          });
        }
      })
    });
    state.jobs["epsilon-one-job"] = {
      jobId: "epsilon-one-job",
      jobKey: "project-1:theorem:epsilon_one",
      status: "formalized",
      declarationName: "even_square_of_even",
      recordedProofPath: dependencyProofPath,
      moduleName: "Lea.Project1.even_square_of_even",
      startedAt: "2026-01-01T00:00:00.000Z",
      finishedAt: "2026-01-01T00:00:01.000Z"
    };

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "epsilon_two",
      targetText: [
        "Theorem name: even_square_of_double_plus_double",
        "Lean signature:",
        "theorem even_square_of_double_plus_double : True := by"
      ].join("\n"),
      targetUses: ["epsilon_one"]
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.equal(result.statusCode, 200);
    assert.match(
      calls[0].body.task,
      new RegExp(`To formalize the theorem make use of the even_square_of_even theorem at ${escapeRegExp(path.join(leaRepo, dependencyProofPath))}\\.`)
    );
    assert.deepEqual(state.jobs[result.body.jobId].targetUses, [{ targetKind: "theorem", targetLabel: "epsilon_one",
      declarationName: "even_square_of_even",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath),
      moduleName: "Lea.Project1.even_square_of_even",
      status: "formalized"
    }]);
    assert.deepEqual(state.jobs[result.body.jobId].formalizationSourceUses, ["epsilon_one"]);
  } finally {
    restorePath();
  }
});

test("formalize includes multiple resolved theorem uses in source order", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const firstProofPath = path.join("workspace", "proofs", "Lea", "Project1", "first_support.lean");
  const secondProofPath = path.join("workspace", "proofs", "Lea", "Project1", "second_support.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "multi_use_target.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(leaRepo, firstProofPath, "theorem first_support : True := by\n  trivial\n");
    await writeLeaProjectProof(leaRepo, secondProofPath, "theorem second_support : True := by\n  trivial\n");
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, targetProofPath, "theorem multi_use_target : True := by\n  trivial\n");
          await writeLeaProjectMarkdown(leaRepo, "project-1", {
            theoremName: "multi_use_target",
            proofPath: targetProofPath
          });
        }
      })
    });
    state.jobs["first-support-job"] = {
      jobId: "first-support-job",
      jobKey: "project-1:theorem:first_label",
      status: "formalized",
      declarationName: "first_support",
      recordedProofPath: firstProofPath,
      startedAt: "2026-01-01T00:00:00.000Z",
      finishedAt: "2026-01-01T00:00:01.000Z"
    };
    state.jobs["second-support-job"] = {
      jobId: "second-support-job",
      jobKey: "project-1:theorem:second_label",
      status: "formalized",
      declarationName: "second_support",
      recordedProofPath: secondProofPath,
      startedAt: "2026-01-01T00:00:02.000Z",
      finishedAt: "2026-01-01T00:00:03.000Z"
    };

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "multi_use_target",
      targetText: "theorem multi_use_target : True := by",
      targetUses: ["first_label", "second_label"],
      targetContext: "Reuse the support lemmas in the listed order."
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    const task = calls[0].body.task;
    const firstIndex = task.indexOf(`make use of the first_support theorem at ${path.join(leaRepo, firstProofPath)}.`);
    const secondIndex = task.indexOf(`make use of the second_support theorem at ${path.join(leaRepo, secondProofPath)}.`);
    assert.notEqual(firstIndex, -1);
    assert.notEqual(secondIndex, -1);
    assert.ok(firstIndex < secondIndex);
    assert.match(task, /Formalization Guidance: Reuse the support lemmas in the listed order\./);
  } finally {
    restorePath();
  }
});

test("formalize allows theorem uses backed by sorry stubs", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const dependencyProofPath = path.join("workspace", "proofs", "Lea", "Project1", "stub_support.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "uses_stub_support.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(leaRepo, dependencyProofPath, "theorem stub_support : True := by\n  sorry\n");
    const artifacts = [];
    const targetStatus = {
      stub_support: ledgerEntry("stub_support", {
        path: "stub_support.lean",
        module_name: "Lea.Project1.stub_support",
        has_sorry: true,
        content: "theorem stub_support : True := by\n  sorry\n"
      })
    };
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        targetStatus,
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, targetProofPath, "import Lea.Project1.stub_support\n\ntheorem uses_stub_support : True := by\n  trivial\n");
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "uses_stub_support",
            path: "uses_stub_support.lean",
            module_name: "Lea.Project1.uses_stub_support",
            kind: "proof"
          });
          targetStatus.uses_stub_support = ledgerEntry("uses_stub_support", {
            path: "uses_stub_support.lean",
            module_name: "Lea.Project1.uses_stub_support",
            content: "import Lea.Project1.stub_support\n\ntheorem uses_stub_support : True := by\n  trivial\n"
          });
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "uses_stub_support",
      targetText: "theorem uses_stub_support : True := by",
      targetUses: ["stub_support"]
    }, state);

    assert.equal(result.statusCode, 200);
    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.match(
      calls[0].body.task,
      new RegExp(`To formalize the theorem make use of the stub_support theorem at ${escapeRegExp(path.join(leaRepo, dependencyProofPath))}\\.`)
    );
    assert.deepEqual(state.jobs[result.body.jobId].targetUses, [{ targetKind: "theorem", targetLabel: "stub_support",
      declarationName: "stub_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath),
      moduleName: "Lea.Project1.stub_support",
      status: "sorry_stub"
    }]);
    assert.deepEqual(state.jobs[result.body.jobId].stubbedTheoremUses, [{ targetKind: "theorem", targetLabel: "stub_support",
      declarationName: "stub_support",
      moduleName: "Lea.Project1.stub_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath)
    }]);
    const statuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "theorem", targetLabel: "uses_stub_support", targetText: "A theorem." }]
    }, state);
    assert.equal(statuses.body.statuses["theorem:uses_stub_support"].status, "formalized");
    assert.deepEqual(statuses.body.statuses["theorem:uses_stub_support"].stubbedTheoremUses, [{ targetKind: "theorem", targetLabel: "stub_support",
      declarationName: "stub_support",
      moduleName: "Lea.Project1.stub_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath)
    }]);
    assert.equal(statuses.body.statuses["theorem:uses_stub_support"].hasStubbedTheoremUses, true);

    await writeLeaProjectProof(leaRepo, dependencyProofPath, "theorem stub_support : True := by\n  trivial\n");
    const refreshedStatuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "theorem", targetLabel: "uses_stub_support", targetText: "A theorem." }]
    }, state);
    assert.equal(refreshedStatuses.body.statuses["theorem:uses_stub_support"].status, "formalized");
    assert.equal(refreshedStatuses.body.statuses["theorem:uses_stub_support"].stubbedTheoremUses, undefined);
    assert.equal(refreshedStatuses.body.statuses["theorem:uses_stub_support"].hasStubbedTheoremUses, undefined);
  } finally {
    restorePath();
  }
});

test("formalized theorem has no stubbed-use warning when stub support is not imported", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const dependencyProofPath = path.join("workspace", "proofs", "Lea", "Project1", "unused_stub_support.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "does_not_import_stub_support.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(leaRepo, dependencyProofPath, "theorem unused_stub_support : True := by\n  sorry\n");
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        targetStatus: {
          unused_stub_support: ledgerEntry("unused_stub_support", {
            path: "unused_stub_support.lean",
            module_name: "Lea.Project1.unused_stub_support",
            has_sorry: true,
            content: "theorem unused_stub_support : True := by\n  sorry\n"
          })
        },
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, targetProofPath, "theorem does_not_import_stub_support : True := by\n  trivial\n");
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "does_not_import_stub_support",
      targetText: "theorem does_not_import_stub_support : True := by",
      targetUses: ["unused_stub_support"]
    }, state);

    assert.equal(result.statusCode, 200);
    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.deepEqual(state.jobs[result.body.jobId].stubbedTheoremUses, []);
    const statuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "theorem", targetLabel: "does_not_import_stub_support", targetText: "A theorem." }]
    }, state);
    assert.equal(statuses.body.statuses["theorem:does_not_import_stub_support"].status, "formalized");
    assert.equal(statuses.body.statuses["theorem:does_not_import_stub_support"].stubbedTheoremUses, undefined);
    assert.equal(statuses.body.statuses["theorem:does_not_import_stub_support"].hasStubbedTheoremUses, undefined);
  } finally {
    restorePath();
  }
});

test("formalized theorem has no stubbed-use warning when imported support is formalized", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const dependencyProofPath = path.join("workspace", "proofs", "Lea", "Project1", "formalized_support.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "imports_formalized_support.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(leaRepo, dependencyProofPath, "theorem formalized_support : True := by\n  trivial\n");
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        targetStatus: {
          formalized_support: ledgerEntry("formalized_support", {
            path: "formalized_support.lean",
            module_name: "Lea.Project1.formalized_support",
            content: "theorem formalized_support : True := by\n  trivial\n"
          })
        },
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, targetProofPath, "import Lea.Project1.formalized_support\n\ntheorem imports_formalized_support : True := by\n  trivial\n");
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "imports_formalized_support",
      targetText: "theorem imports_formalized_support : True := by",
      targetUses: ["formalized_support"]
    }, state);

    assert.equal(result.statusCode, 200);
    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.deepEqual(state.jobs[result.body.jobId].stubbedTheoremUses, []);
    assert.deepEqual(state.jobs[result.body.jobId].targetUses, [{ targetKind: "theorem", targetLabel: "formalized_support",
      declarationName: "formalized_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath),
      moduleName: "Lea.Project1.formalized_support",
      status: "formalized"
    }]);
  } finally {
    restorePath();
  }
});

test("formalize allows theorem uses backed by failed sorry stubs", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const dependencyProofPath = path.join("workspace", "proofs", "Lea", "Project1", "failed_stub_support.lean");
  const targetProofPath = path.join("workspace", "proofs", "Lea", "Project1", "uses_failed_stub_support.lean");
  const restorePath = await installFakeLake();
  try {
    await writeLeaProjectProof(leaRepo, dependencyProofPath, "theorem failed_stub_support : True := by\n  sorry\n");
    const artifacts = [];
    const targetStatus = {
      failed_stub_support: ledgerEntry("failed_stub_support", {
        path: "failed_stub_support.lean",
        module_name: "Lea.Project1.failed_stub_support",
        has_sorry: true,
        content: "theorem failed_stub_support : True := by\n  sorry\n"
      })
    };
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        targetStatus,
        onStatusRequest: async () => {
          await writeLeaProjectProof(leaRepo, targetProofPath, "import Lea.Project1.failed_stub_support\n\ntheorem uses_failed_stub_support : True := by\n  trivial\n");
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "uses_failed_stub_support",
            path: "uses_failed_stub_support.lean",
            module_name: "Lea.Project1.uses_failed_stub_support",
            kind: "proof"
          });
          targetStatus.uses_failed_stub_support = ledgerEntry("uses_failed_stub_support", {
            path: "uses_failed_stub_support.lean",
            module_name: "Lea.Project1.uses_failed_stub_support",
            content: "import Lea.Project1.failed_stub_support\n\ntheorem uses_failed_stub_support : True := by\n  trivial\n"
          });
        }
      })
    });
    state.jobs.failed_stub_job = {
      jobId: "failed_stub_job",
      jobKey: "project-1:theorem:failed_stub_support",
      status: "failed",
      finalStatus: "sorry_stub",
      targetLabel: "failed_stub_support",
      declarationName: "failed_stub_support",
      recordedProofPath: dependencyProofPath,
      moduleName: "Lea.Project1.failed_stub_support",
      relativePath: path.join("workspace", "projects", "project-1.md"),
      absolutePath: path.join(leaRepo, "workspace", "projects", "project-1.md"),
      logPath: path.join(path.dirname(state.jobsPath), "failed-stub-support.log"),
      leaRepoPath: leaRepo,
      startedAt: "2026-01-01T00:00:00.000Z",
      finishedAt: "2026-01-01T00:00:01.000Z"
    };
    await fs.writeFile(state.jobs.failed_stub_job.logPath, "failed after stub\n", "utf8");

    const statuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "theorem", targetLabel: "failed_stub_support", targetText: "A theorem." }]
    }, state);

    assert.equal(statuses.statusCode, 200);
    // File truth wins over the failed run's lifecycle record (the settled
    // 4.4 semantic): the stub on disk is real, compiles, and is usable via
    // `uses=`, so it reads sorry_stub — the failed attempt stays in the job
    // history, it just doesn't masquerade as the state of the artifact.
    assert.equal(statuses.body.statuses["theorem:failed_stub_support"].status, "sorry_stub");
    assert.equal(statuses.body.statuses["theorem:failed_stub_support"].leanStatement, "theorem failed_stub_support : True");

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "uses_failed_stub_support",
      targetText: "theorem uses_failed_stub_support : True := by",
      targetUses: ["failed_stub_support"]
    }, state);

    assert.equal(result.statusCode, 200);
    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.match(
      calls[0].body.task,
      new RegExp(`To formalize the theorem make use of the failed_stub_support theorem at ${escapeRegExp(path.join(leaRepo, dependencyProofPath))}\\.`)
    );
    assert.deepEqual(state.jobs[result.body.jobId].targetUses, [{ targetKind: "theorem", targetLabel: "failed_stub_support",
      declarationName: "failed_stub_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath),
      moduleName: "Lea.Project1.failed_stub_support",
      status: "sorry_stub"
    }]);
    assert.deepEqual(state.jobs[result.body.jobId].stubbedTheoremUses, [{ targetKind: "theorem", targetLabel: "failed_stub_support",
      declarationName: "failed_stub_support",
      moduleName: "Lea.Project1.failed_stub_support",
      relativePath: dependencyProofPath,
      absolutePath: path.join(leaRepo, dependencyProofPath)
    }]);
  } finally {
    restorePath();
  }
});

test("formalize blocks unresolved theorem uses", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "needs_support",
    targetText: "A theorem.",
    targetUses: ["missing_support"]
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "unresolved_uses");
  assert.match(result.body.message, /missing_support/);
  assert.equal(calls.length, 0);
});

test("formalize rejects invalid theorem uses labels", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" }
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "needs_support",
    targetText: "A theorem.",
    targetUses: ["invalid-label"]
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "invalid_uses");
});

test("formalize maps unnamed theorem output that uses the Overleaf label", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "label_named_result.lean");
  const restorePath = await installFakeLake();
  try {
    const artifacts = [];
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        onStatusRequest: async () => {
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "theorem label_named_result : True := by\n  trivial\n"
          );
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "label_named_result",
            path: "label_named_result.lean",
            module_name: "Lea.Project1.label_named_result",
            kind: "proof"
          });
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "label_named_result",
      targetText: "A theorem without a Lean name."
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.equal(state.jobs[result.body.jobId].declarationName, "label_named_result");
    assert.equal(state.jobs[result.body.jobId].recordedProofPath, proofPath);
  } finally {
    restorePath();
  }
});

test("formalize persists completed Lea usage and cost", async () => {
  const leaRepo = await makeLeaRepo();
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "usage_capture_test.lean");
  const restorePath = await installFakeLake();
  try {
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch([], {
        statusBody: {
          run_id: "api-run-1",
          status: "completed",
          result: {
            reason: "success",
            usage: { input_tokens: 1234, output_tokens: 567 },
            cost: 0.42
          }
        },
        onStatusRequest: async () => {
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "theorem usage_capture_test : True := by\n  trivial\n"
          );
          await writeLeaProjectMarkdown(leaRepo, "project-1", {
            theoremName: "usage_capture_test",
            proofPath
          });
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "usage_capture_test",
      targetText: "A theorem."
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    assert.deepEqual(state.jobs[result.body.jobId].usage, {
      inputTokens: 1234,
      outputTokens: 567,
      totalTokens: 1801
    });
    assert.equal(state.jobs[result.body.jobId].costUsd, 0.42);
  } finally {
    restorePath();
  }
});

test("formalize definition uses a declaration-oriented prompt and typed job key", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "Subadditive.lean");
  const restorePath = await installFakeLake();
  try {
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        onStatusRequest: async () => {
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "def Subadditive (a : Nat -> Int) : Prop := True\n"
          );
          await writeLeaProjectMarkdown(leaRepo, "project-1", {
            theoremName: "Subadditive",
            proofPath,
            moduleName: "Lea.Project1.Subadditive"
          });
        }
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "definition",
      targetLabel: "Subadditive",
      targetText: "A sequence is subadditive when a_{m+n} <= a_m + a_n.",
      targetContext: "Represent this as a predicate on functions Nat -> Int."
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    const job = state.jobs[result.body.jobId];
    assert.equal(result.statusCode, 200);
    assert.equal(job.jobKey, "project-1:definition:Subadditive");
    assert.equal(job.targetKind, "definition");
    assert.equal(job.targetLabel, "Subadditive");
    assert.equal(job.resultKind, "defined");
    assert.match(calls[0].body.task, /This target is a definition, not a theorem/);
    assert.match(calls[0].body.task, /Do not create a fake theorem/);
    assert.match(calls[0].body.task, /Represent this as a predicate/);

    const statuses = await handleGetStatuses({
      overleafProjectId: "project-1",
      targets: [{ targetKind: "definition", targetLabel: "Subadditive", targetText: "A definition." }]
    }, state);
    assert.equal(statuses.body.statuses["definition:Subadditive"].status, "formalized");
    assert.equal(statuses.body.statuses["definition:Subadditive"].resultKind, "defined");
  } finally {
    restorePath();
  }
});

test("same-label theorem and definition targets do not collide", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  state.jobs.same_theorem = {
    jobId: "same_theorem",
    jobKey: "project-1:theorem:SameName",
    status: "in_progress",
    mode: "formalization",
    targetKind: "theorem",
    targetLabel: "SameName",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    declarationName: "SameName",
    projectMarkdownPath: buildLeaProjectMarkdownPath({ leaRepoPath: leaRepo, overleafProjectId: "project-1" }),
    leaRepoPath: leaRepo,
    leaUiBaseUrl: "http://localhost:5173",
    startedAt: "2026-01-01T00:00:00.000Z"
  };
  state.jobs.same_definition = {
    ...state.jobs.same_theorem,
    jobId: "same_definition",
    jobKey: "project-1:definition:SameName",
    targetKind: "definition",
    status: "failed",
    finalStatus: "unformalized",
    error: "definition failed",
    startedAt: "2026-01-01T00:00:01.000Z"
  };

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [
      { targetKind: "theorem", targetLabel: "SameName", targetText: "A theorem." },
      { targetKind: "definition", targetLabel: "SameName", targetText: "A definition." }
    ]
  }, state);

  assert.equal(statuses.body.statuses["theorem:SameName"].status, "in_progress");
  assert.equal(statuses.body.statuses["definition:SameName"].status, "failed");
  assert.equal(statuses.body.statuses["definition:SameName"].message, "definition failed");
});

test("usage falls back to in-memory job totals when the adapter is unavailable", async () => {
  const leaRepo = await makeLeaRepo();
  // With the adapter unavailable, handleGetUsage uses the in-memory job tally
  // fallback rather than GET /api/stats.
  const state = await makeState({ leaRepoPath: leaRepo, leaMaxSpendUsd: 1 });
  state.jobs.project_a_first = makeUsageJob({
    jobId: "project_a_first",
    projectId: "project-a",
    inputTokens: 100,
    outputTokens: 25,
    costUsd: 0.12
  });
  state.jobs.project_a_second = makeUsageJob({
    jobId: "project_a_second",
    projectId: "project-a",
    inputTokens: 300,
    outputTokens: 75,
    costUsd: 0.34
  });
  state.jobs.project_b = makeUsageJob({
    jobId: "project_b",
    projectId: "project-b",
    inputTokens: 50,
    outputTokens: 10,
    costUsd: 0.02
  });
  state.jobs.old_without_usage = {
    jobId: "old_without_usage",
    status: "formalized",
    overleafProjectId: "project-a",
    projectSlug: "project-a"
  };

  const result = await handleGetUsage({ overleafProjectId: "project-a" }, state);

  assert.equal(result.statusCode, 200);
  assert.deepEqual(result.body.project, {
    inputTokens: 400,
    outputTokens: 100,
    totalTokens: 500,
    costUsd: 0.46,
    runCount: 2
  });
  assert.deepEqual(result.body.allTime, {
    inputTokens: 450,
    outputTokens: 110,
    totalTokens: 560,
    costUsd: 0.48,
    runCount: 3
  });
  assert.equal(result.body.leaMaxSpendUsd, 1);
  assert.equal(result.body.leaCurrentSpendUsd, 0.48);
  assert.equal(result.body.leaSpendLimitReached, false);
});

test("usage sourced from the adapter matches /api/stats (all-time + this project)", async () => {
  const leaRepo = await makeLeaRepo();
  const projectSlug = slugProjectId("doc-a");
  const stats = {
    global: {
      input_tokens: 1000,
      output_tokens: 250,
      total_tokens: 1250,
      cost_usd: 0.7,
      session_count: 4
    },
    sessions: [
      // Two sessions for this Overleaf doc → summed into "This project".
      { project_slug: projectSlug, input_tokens: 200, output_tokens: 50, cost_usd: 0.10, run_count: 1 },
      { project_slug: projectSlug, input_tokens: 300, output_tokens: 75, cost_usd: 0.20, run_count: 2 },
      // A different doc and an untagged session → excluded from "This project".
      { project_slug: slugProjectId("doc-b"), input_tokens: 400, output_tokens: 100, cost_usd: 0.30, run_count: 1 },
      { project_slug: null, input_tokens: 100, output_tokens: 25, cost_usd: 0.10, run_count: 1 }
    ]
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaMaxSpendUsd: 1,
    fetchImpl: makeStatsFetch(stats)
  });
  // An in-memory job exists but must be ignored once the adapter is the source.
  state.jobs.stale = makeUsageJob({
    jobId: "stale", projectId: "doc-a", inputTokens: 9, outputTokens: 9, costUsd: 9
  });

  const result = await handleGetUsage({ overleafProjectId: "doc-a" }, state);

  assert.equal(result.statusCode, 200);
  assert.deepEqual(result.body.project, {
    inputTokens: 500,
    outputTokens: 125,
    totalTokens: 625,
    costUsd: 0.3,
    runCount: 3
  });
  assert.deepEqual(result.body.allTime, {
    inputTokens: 1000,
    outputTokens: 250,
    totalTokens: 1250,
    costUsd: 0.7,
    runCount: 4
  });
  assert.equal(result.body.leaCurrentSpendUsd, 0.7);
  assert.equal(result.body.leaSpendLimitReached, false);
});

test("usage from the adapter flags the spend cap from all-time cost", async () => {
  const leaRepo = await makeLeaRepo();
  const stats = {
    global: { input_tokens: 10, output_tokens: 5, total_tokens: 15, cost_usd: 0.5, session_count: 1 },
    sessions: []
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaMaxSpendUsd: 0.5,
    fetchImpl: makeStatsFetch(stats)
  });

  const result = await handleGetUsage({ overleafProjectId: "doc-a" }, state);

  assert.equal(result.body.leaCurrentSpendUsd, 0.5);
  assert.equal(result.body.leaSpendLimitReached, true);
});

test("formalize cleans previous failed Lea artifacts before retrying", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "retry_target.lean");
  await writeLeaProjectProof(
    leaRepo,
    proofPath,
    "theorem retry_target : True := by\n  trivial\n"
  );
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "retry_target",
    proofPath
  });

  const restorePath = await installFakeLake();
  try {
    // The retire candidates come from the adapter's ledger (4.3) — the
    // registry markdown is only the agent-facing view the cleanup splices.
    const artifacts = [];
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeLeaApiFetch(calls, {
        statusBody: { run_id: "api-run-1", status: "completed", result: { reason: "success" } },
        artifacts,
        targetStatus: {
          retry_target: ledgerEntry("retry_target", {
            path: "retry_target.lean",
            module_name: "Lea.Project1.retry_target",
            content: "theorem retry_target : True := by\n  trivial\n"
          })
        },
        onStatusRequest: async () => {
          assert.equal(await fileExists(path.join(leaRepo, proofPath)), false);
          assert.doesNotMatch(
            await fs.readFile(path.join(leaRepo, "workspace", "projects", "project-1.md"), "utf8"),
            /retry_target/
          );
          await writeLeaProjectProof(
            leaRepo,
            proofPath,
            "theorem retry_target : True := by\n  trivial\n"
          );
          await writeLeaProjectMarkdown(leaRepo, "project-1", {
            theoremName: "retry_target",
            proofPath
          });
          artifacts.push({
            run_id: "api-run-1",
            declaration_name: "retry_target",
            path: "retry_target.lean",
            module_name: "Lea.Project1.retry_target",
            kind: "proof"
          });
        }
      })
    });
    state.jobs.previous_failed = {
      jobId: "previous_failed",
      jobKey: "project-1:theorem:retry_label",
      status: "failed",
      targetLabel: "retry_label",
      declarationName: "retry_label",
      declarationNameHint: "retry_target",
      relativePath: path.join("workspace", "projects", "project-1.md"),
      absolutePath: path.join(leaRepo, "workspace", "projects", "project-1.md"),
      logPath: path.join(path.dirname(state.jobsPath), "previous-failed.log"),
      leaRepoPath: leaRepo,
      startedAt: "2026-01-01T00:00:00.000Z",
      finishedAt: "2026-01-01T00:00:01.000Z"
    };

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
    targetLabel: "retry_label",
      targetText: [
        "Theorem name: retry_target",
        "Lean signature:",
        "theorem retry_target : True := by"
      ].join("\n")
    }, state);

    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
    const job = state.jobs[result.body.jobId];
    assert.deepEqual(job.retryCleanup.removedProofPaths, [proofPath]);
    assert.deepEqual(job.retryCleanup.removedProjectEntries, ["retry_target"]);
    assert.equal(job.declarationName, "retry_target");
    assert.equal(job.recordedProofPath, proofPath);
  } finally {
    restorePath();
  }
});

test("statuses are unformalized when project markdown has no theorem entry", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "other_theorem",
    proofPath: path.join("workspace", "proofs", "Lea", "Project1", "Other.lean")
  });

  const result = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "missing_entry_test", targetText: "A theorem." }]
  }, state);

  const status = result.body.statuses["theorem:missing_entry_test"];
  assert.equal(result.statusCode, 200);
  assert.equal(status.status, "unformalized");
  assert.equal(status.projectSlug, "project-1");
});

// The settled retry-restore semantic (PLAN 4.4, decided at the default flip):
// valid ledger evidence beats a stale failed job — after a failed retry
// restores the previous verified proof, status reports the restored file's
// validity, not the failed run.
test("ledger evidence for a valid recorded file overrides a stale failed job", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], {
      targetStatus: { failed_but_written_test: ledgerEntry("failed_but_written_test") }
    })
  });
  state.jobs.failed_job = {
    jobId: "failed_job",
    jobKey: "project-1:theorem:failed_but_written_test",
    status: "failed",
    targetLabel: "failed_but_written_test",
    declarationName: "failed_but_written_test",
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    logPath: path.join(path.dirname(state.jobsPath), "failed-but-written.log"),
    leaRepoPath: leaRepo,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };
  await fs.writeFile(state.jobs.failed_job.logPath, "failed proof\n", "utf8");

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "failed_but_written_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:failed_but_written_test"].status, "formalized");
});

test("formalize rejects missing OpenAI key", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: {}
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "missing_key_test",
    targetText: "A theorem."
  }, state);

  assert.equal(result.statusCode, 400);
  assert.equal(result.body.error, "missing_openai_key");
});

test("failed jobs take precedence over completed project markdown entries", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "FailedPrecedence.lean");
  await writeLeaProjectProof(leaRepo, proofPath, "theorem failed_precedence_test : True := by\n  trivial\n");
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "failed_precedence_test",
    proofPath
  });
  state.jobs.failed_job = {
    jobId: "failed_job",
    jobKey: "project-1:theorem:failed_precedence_test",
    status: "failed",
    targetLabel: "failed_precedence_test",
    relativePath: path.join("workspace", "projects", "project-1.md"),
    absolutePath: path.join(leaRepo, "workspace", "projects", "project-1.md"),
    logPath: path.join(path.dirname(state.jobsPath), "failed-precedence.log"),
    leaRepoPath: leaRepo,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };
  await fs.writeFile(state.jobs.failed_job.logPath, "failed proof\n", "utf8");

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "failed_precedence_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:failed_precedence_test"].status, "failed");
  assert.equal(statuses.body.statuses["theorem:failed_precedence_test"].effectiveStatus, "unformalized");
  assert.match(statuses.body.statuses["theorem:failed_precedence_test"].logTail, /failed proof/);
});

test("formalize fails a Lea job that exceeds the job timeout", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaJobTimeoutSeconds: 0.01,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([], { neverDone: true })
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "timeout_test",
    targetText: "A theorem."
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "in_progress");
  await waitFor(() => state.jobs[result.body.jobId]?.status === "failed");

  const job = state.jobs[result.body.jobId];
  assert.equal(job.timedOut, true);
  assert.equal(job.exitCode, 1);
  assert.match(job.error, /timed out/);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "timeout_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:timeout_test"].status, "failed");
  assert.match(statuses.body.statuses["theorem:timeout_test"].logTail, /timed out/);
});

test("startup recovery fails interrupted in-progress jobs", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });
  const logPath = path.join(path.dirname(state.jobsPath), "interrupted.log");
  state.jobs.interrupted_job = {
    jobId: "interrupted_job",
    jobKey: "project-1:theorem:interrupted_status_test",
    status: "in_progress",
    targetLabel: "interrupted_status_test",
    relativePath: path.join("workspace", "projects", "project-1.md"),
    absolutePath: path.join(leaRepo, "workspace", "projects", "project-1.md"),
    logPath,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: null
  };

  await recoverInterruptedJobs(state);

  assert.equal(state.jobs.interrupted_job.status, "failed");
  assert.equal(state.jobs.interrupted_job.finalStatus, "interrupted");
  assert.match(state.jobs.interrupted_job.error, /Companion restarted/);
  assert.ok(state.jobs.interrupted_job.finishedAt);
  assert.match(await fs.readFile(logPath, "utf8"), /Companion restarted/);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "interrupted_status_test", targetText: "A theorem." }]
  }, state);

  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:interrupted_status_test"].status, "failed");
});

async function makeLeaRepo() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-prover-"));
  await fs.mkdir(path.join(dir, "lea_api"), { recursive: true });
  await fs.mkdir(path.join(dir, "workspace", "projects"), { recursive: true });
  await fs.mkdir(path.join(dir, "workspace", "proofs"), { recursive: true });
  await fs.writeFile(path.join(dir, "pyproject.toml"), "[project]\nname = \"lea-prover\"\n", "utf8");
  await fs.writeFile(path.join(dir, "workspace", "lean-toolchain"), "leanprover/lean4:stable\n", "utf8");
  await fs.writeFile(path.join(dir, "workspace", "lakefile.lean"), "import Lake\nopen Lake DSL\n", "utf8");
  return dir;
}

async function writeLeaProjectProof(leaRepo, proofPath, content) {
  const absolutePath = path.join(leaRepo, proofPath);
  await fs.mkdir(path.dirname(absolutePath), { recursive: true });
  await fs.writeFile(absolutePath, content, "utf8");
}

async function writeLeaProjectMarkdown(leaRepo, projectId, { theoremName, proofPath, moduleName = null }) {
  await writeLeaProjectMarkdownEntries(leaRepo, projectId, [{ theoremName, proofPath, moduleName }]);
}

async function writeLeaProjectMarkdownEntries(leaRepo, projectId, entries) {
  const markdownPath = path.join(leaRepo, "workspace", "projects", `${slugProjectId(projectId)}.md`);
  await fs.mkdir(path.dirname(markdownPath), { recursive: true });
  await fs.writeFile(
    markdownPath,
    [
      `# Project ${projectId}`,
      "",
      `<!-- lea:project id="${slugProjectId(projectId)}" -->`,
      "",
      ...entries.flatMap(({ theoremName, proofPath, moduleName = null }) => {
        const moduleAttr = moduleName ? ` module="${moduleName}"` : "";
        return [
          `## Theorem: ${theoremName}`,
          "",
          `<!-- lea:theorem name="${theoremName}" proof="${proofPath}"${moduleAttr} -->`,
          "",
          "### Signature",
          "",
          "```lean",
          `theorem ${theoremName} : True := by`,
          "```",
          ""
        ];
      }),
      ""
    ].join("\n"),
    "utf8"
  );
}

async function installFakeLake() {
  const oldPath = process.env.PATH || "";
  const binDir = await fs.mkdtemp(path.join(os.tmpdir(), "fake-lake-bin-"));
  const lakePath = path.join(binDir, "lake");
  await fs.writeFile(lakePath, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  process.env.PATH = `${binDir}${path.delimiter}${oldPath}`;
  return () => {
    process.env.PATH = oldPath;
  };
}

async function makeState(overrides = {}) {
  const appDir = await fs.mkdtemp(path.join(os.tmpdir(), "overleaf-lean-state-"));
  return {
    settingsPath: path.join(appDir, "settings.json"),
    jobsPath: path.join(appDir, "jobs.json"),
    envPath: path.join(appDir, ".env"),
    settings: {
      ...(overrides.leaRepoPath ? {
        leaRepoPath: overrides.leaRepoPath,
        leaWorkspacePath: buildLeaWorkspacePath(overrides.leaRepoPath),
        leaApiBaseUrl: overrides.leaApiBaseUrl || "http://127.0.0.1:8001",
        leaProvider: overrides.leaProvider || "openai",
        leaModel: overrides.leaModel || "o4-mini",
        leaProviderApiKeys: overrides.leaProviderApiKeys || {},
        ...(overrides.leaApiKey ? { leaApiKey: overrides.leaApiKey } : {}),
        leaMaxTurns: 20,
        leaMaxSpendUsd: overrides.leaMaxSpendUsd ?? null,
        leaTexMirrorEnabled: overrides.leaTexMirrorEnabled ?? true,
        ...(overrides.leaJobTimeoutSeconds ? {
          leaJobTimeoutSeconds: overrides.leaJobTimeoutSeconds
        } : {})
      } : {})
    },
    jobs: {},
    env: overrides.env || process.env,
    fetchImpl: overrides.fetchImpl || makeProviderValidationFetch([])
  };
}

function makeUsageJob({ jobId, projectId, inputTokens, outputTokens, costUsd }) {
  return {
    jobId,
    status: "formalized",
    overleafProjectId: projectId,
    projectSlug: slugProjectId(projectId),
    usage: {
      inputTokens,
      outputTokens,
      totalTokens: inputTokens + outputTokens
    },
    costUsd,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };
}

async function fileExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function makeLeaApiFetch(calls, options = {}) {
  let eventHookHandled = false;
  return async (url, requestOptions = {}) => {
    if (String(url).endsWith("/api/settings")) {
      return jsonResponse(404, { detail: "not found" });
    }
    if (String(url).includes("/api/projects/by-slug/") && String(url).endsWith("/identity")) {
      return jsonResponse(404, { detail: "No project" });
    }
    // Adapter index + ledger evidence (PLAN 4.1–4.4). Answered BEFORE the
    // calls.push so tests keyed to calls[0] still see the run POST first.
    // `artifacts`/`targetStatus` are read at call time, so a test's
    // onStatusRequest hook can populate them mid-"run" the way the real
    // finalizer does; absent options emulate an adapter without the index.
    if (String(url).includes("/api/projects/by-slug/") && String(url).endsWith("/artifacts")) {
      return options.artifacts
        ? jsonResponse(200, { project_id: "adapter-project-1", slug: "project-1", artifacts: options.artifacts })
        : jsonResponse(404, { detail: "No project" });
    }
    if (String(url).includes("/api/projects/by-slug/") && String(url).includes("/target-status")) {
      const requested = decodeURIComponent(String(url).split("declarations=")[1] || "").split(",").filter(Boolean);
      const known = options.targetStatus || {};
      return jsonResponse(200, {
        project_id: "adapter-project-1",
        slug: "project-1",
        targets: requested.map((name) => known[name] || { declaration_name: name, recorded: false })
      });
    }
    const body = requestOptions.body ? JSON.parse(requestOptions.body) : null;
    const recordedBody = body?.message ? { ...body, task: body.message } : body;
    calls.push({ url, options: requestOptions, body: recordedBody });
    if (String(url).endsWith("/api/runs") && requestOptions.method === "POST") {
      return jsonResponse(200, { run_id: "api-run-1", session_id: "sess-api-1", status: "running" });
    }
    if (String(url).includes("/api/runs/") && String(url).endsWith("/events")) {
      if (!eventHookHandled && options.onStatusRequest) {
        eventHookHandled = true;
        await options.onStatusRequest();
      }
      if (options.neverDone) {
        return {
          ok: true,
          status: 200,
          body: (async function* () {
            await new Promise((_, reject) => {
              requestOptions.signal?.addEventListener("abort", () => {
                const error = new Error("aborted");
                error.name = "AbortError";
                reject(error);
              }, { once: true });
            });
          })()
        };
      }
      // Legacy fixtures say reason "success"; the adapter vocabulary has no
      // such status (the client alias was removed once the integration
      // harness pinned the real wire values), so the fake maps it to the real
      // "proved" in one place instead of touching every fixture.
      const rawDone = options.statusBody?.result?.reason || options.doneStatus || "proved";
      const frames = options.eventFrames
        ? options.eventFrames.map((event) => ({
            type: event.type === "usage_updated" ? "status" : event.type,
            data: event
          }))
        : [
            { type: "status", data: { status: "tool_call", turn: 1 } },
            { type: "done", data: { status: rawDone === "success" ? "proved" : rawDone } }
          ];
      return adapterSseResponse(frames);
    }
    if (String(url).includes("/api/sessions/")) {
      const usage = options.statusBody?.result?.usage || {};
      return jsonResponse(200, {
        runs: [{
          id: "api-run-1",
          status: options.doneStatus || "proved",
          result_kind: options.doneStatus || "proved",
          input_tokens: usage.input_tokens || 0,
          output_tokens: usage.output_tokens || 0,
          cost_usd: options.statusBody?.result?.cost || 0
        }],
        // extra session-detail fields (project_namespace, code_steps) for
        // tests that exercise the unregistered-artifact session recovery
        ...(options.sessionBody || {})
      });
    }
    if (String(url).endsWith("/interrupt")) {
      return jsonResponse(options.cancelStatus || 200, options.cancelBody || { status: "interrupting" });
    }
    return jsonResponse(404, { detail: "not found" });
  };
}

// Serves the lea-standalone adapter's GET /api/stats with a fixed payload so the
// popover-usage path can be exercised without a live adapter.
function makeStatsFetch(stats) {
  return async (url) => {
    if (String(url).endsWith("/api/stats")) {
      return jsonResponse(200, stats);
    }
    return jsonResponse(404, { detail: "not found" });
  };
}

function keyStatus(last4, label) {
  return { configured: true, last4, label: label || null };
}

function makeAdapterStore(overrides = {}) {
  return {
    settings: {
      model: overrides.model || "o4-mini",
      max_turns: overrides.max_turns ?? 20,
      max_spend_usd: overrides.max_spend_usd ?? null,
      current_spend_usd: 0,
      api_keys: overrides.api_keys || {},
      model_options: []
    },
    models: overrides.models || [
      { value: "o4-mini", label: "o4-mini", provider: "openai" },
      { value: "mistral/mistral-large-latest", label: "mistral/mistral-large-latest", provider: "mistral" }
    ],
    requirements: overrides.requirements || null,
    reject: overrides.reject || null
  };
}

// Simulates the lea-standalone adapter's GET/PUT /api/settings, plus falls through
// to provider-key validation URLs (api.openai.com, …) for the companion's own
// pre-save key checks.
function makeAdapterFetch(adapter, calls = []) {
  return async (url, requestOptions = {}) => {
    const u = String(url);
    if (u.includes("/api/models/requirements")) {
      const model = new URL(u).searchParams.get("model") || "";
      calls.push({ method: "GET", url: u, model });
      if (adapter.requirements) return jsonResponse(200, adapter.requirements(model, adapter.settings));
      const provider = model.startsWith("anthropic/") || model.startsWith("claude-")
        ? "anthropic"
        : model.startsWith("gemini/")
          ? "gemini"
          : model.startsWith("mistral/")
            ? "mistral"
            : "openai";
      const env = provider === "anthropic"
        ? "ANTHROPIC_API_KEY"
        : provider === "gemini"
          ? "GOOGLE_API_KEY"
          : provider === "mistral"
            ? "MISTRAL_API_KEY"
            : "OPENAI_API_KEY";
      const configured = Boolean(adapter.settings.api_keys?.[env]?.configured);
      return jsonResponse(200, {
        model,
        provider,
        required_keys: [{ env, label: provider, configured }],
        satisfied: configured
      });
    }
    if (u.endsWith("/api/models")) {
      calls.push({ method: "GET", url: u });
      return jsonResponse(200, { models: adapter.models });
    }
    if (u.endsWith("/api/settings")) {
      const method = requestOptions.method || "GET";
      if (method === "GET") {
        calls.push({ method: "GET", url: u });
        return jsonResponse(200, adapter.settings);
      }
      if (method === "PUT") {
        const body = requestOptions.body ? JSON.parse(requestOptions.body) : {};
        calls.push({ method: "PUT", url: u, body });
        if (adapter.reject) {
          return jsonResponse(422, { detail: { message: adapter.reject.message, field: adapter.reject.field } });
        }
        if (typeof body.max_turns === "number") adapter.settings.max_turns = body.max_turns;
        if (body.max_spend_usd === null || typeof body.max_spend_usd === "number") {
          adapter.settings.max_spend_usd = body.max_spend_usd;
        }
        if (body.model) adapter.settings.model = body.model;
        for (const [env, patch] of Object.entries(body.api_keys || {})) {
          if (patch?.value) adapter.settings.api_keys[env] = keyStatus(String(patch.value).slice(-4), env);
        }
        return jsonResponse(200, adapter.settings);
      }
    }
    return makeProviderValidationFetch(calls)(url, requestOptions);
  };
}

function makeProviderValidationFetch(calls, statuses = {}) {
  return async (url, requestOptions = {}) => {
    const family = inferProviderValidationFamily(url);
    if (!family) {
      throw new Error(`Unexpected provider validation URL: ${url}`);
    }
    calls.push({ family, url, options: requestOptions });
    const status = statuses[family] || 200;
    if (status === "throw") {
      throw new Error(`${family} unavailable`);
    }
    return jsonResponse(status, { data: [] });
  };
}

function inferProviderValidationFamily(url) {
  const text = String(url);
  if (text.startsWith("https://api.openai.com/")) return "openai";
  if (text.startsWith("https://generativelanguage.googleapis.com/")) return "google";
  if (text.startsWith("https://api.anthropic.com/")) return "anthropic";
  return "";
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(body);
    }
  };
}

async function waitFor(predicate) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > 3000) {
      throw new Error("Timed out waiting for condition.");
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

// An adapter-style SSE response: the event name rides the `event:` line.
function adapterSseResponse(frames) {
  return {
    ok: true,
    status: 200,
    body: (async function* () {
      for (const { type, data } of frames) {
        yield new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
      }
    })()
  };
}

function makeAdapterApiFetch(calls, options = {}) {
  return async (url, requestOptions = {}) => {
    const body = requestOptions.body ? JSON.parse(requestOptions.body) : null;
    calls.push({ url, options: requestOptions, body });
    if (String(url).endsWith("/api/runs") && requestOptions.method === "POST") {
      return jsonResponse(200, {
        session_id: options.sessionId || "sess-api-1",
        run_id: options.runId || "api-run-1",
        message: {},
        project_id: options.projectId || "adapter-project-1",
        project_slug: options.projectSlug || body?.project_slug || null,
        project_namespace: options.projectNamespace || "Lea.Project1"
      });
    }
    if (String(url).includes("/api/runs/") && String(url).endsWith("/events")) {
      return adapterSseResponse(options.eventFrames || [
        { type: "status", data: { status: "tool_call", message: "Running write_file", turn: 1 } },
        { type: "done", data: { status: options.doneStatus || "proved" } }
      ]);
    }
    if (String(url).includes("/approvals/")) {
      return jsonResponse(200, { status: "resolved", decision: "always_session" });
    }
    if (String(url).includes("/api/sessions/")) {
      return jsonResponse(200, options.sessionDetail || {
        runs: [{
          id: options.runId || "api-run-1",
          status: options.doneStatus || "proved",
          result_kind: options.doneStatus || "proved",
          input_tokens: 10,
          output_tokens: 5,
          cost_usd: 0.001
        }],
        code_steps: []
      });
    }
    if (String(url).includes("/api/projects/by-slug/") && String(url).endsWith("/artifacts")) {
      // The structured artifact index (PLAN 4.1/4.2); absent option = adapter
      // without the index (404) → companion falls back to markdown diffing.
      return options.artifacts
        ? jsonResponse(200, { project_id: "adapter-project-1", slug: "project-1", artifacts: options.artifacts })
        : jsonResponse(404, { detail: "No project" });
    }
    if (String(url).includes("/api/projects/by-slug/") && String(url).includes("/target-status")) {
      // Ledger evidence for the ledger status engine (PLAN 4.4). The option
      // maps declaration name → entry; unnamed declarations answer
      // { recorded: false } like the real endpoint.
      const requested = decodeURIComponent(String(url).split("declarations=")[1] || "").split(",").filter(Boolean);
      const known = options.targetStatus || {};
      return jsonResponse(200, {
        project_id: "adapter-project-1",
        slug: "project-1",
        targets: requested.map((name) => known[name] || { declaration_name: name, recorded: false })
      });
    }
    if (String(url).endsWith("/interrupt")) {
      return jsonResponse(200, { status: "interrupting" });
    }
    throw new Error(`unexpected adapter fetch: ${url}`);
  };
}

test("formalize on the /api backend posts to /api/runs and runs autonomously (no /v1 calls)", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls)
  });
  state.projectIdentities = {
    "project-1": {
      slug: "project-1",
      projectName: "Renamed Project",
      namespace: "Lea.RenamedProject"
    }
  };

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "t_api",
    targetText: "A theorem.",
    projectName: "Original Project"
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "in_progress");
  await waitFor(() => calls.some((c) => String(c.url).endsWith("/api/runs") && c.options?.method === "POST"));
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.ok(runCall.body.message.includes("Project display name: Renamed Project"));
  assert.ok(runCall.body.message.includes("Lean namespace: Lea.RenamedProject"));
  assert.ok(runCall.body.message.includes("Overleaf binding: project-1"));
  assert.ok(runCall.body.message.includes("do not derive a namespace from the display name"));
  assert.ok(!runCall.body.message.includes("in project project-1"));
  assert.equal(runCall.body.project_title, "Renamed Project");
  assert.equal(runCall.body.project_namespace, "Lea.RenamedProject");
  assert.ok(!calls.some((c) => String(c.url).includes("/v1/")));
});

test("formalize fetches adapter identity after companion restart before composing the prompt", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapterFetch = makeAdapterApiFetch(calls);
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: async (url, options = {}) => {
      calls.push({ url: String(url), options, body: options.body ? JSON.parse(options.body) : null });
      if (String(url).endsWith("/api/projects/by-slug/project-1/identity") && options.method === "GET") {
        return jsonResponse(200, {
          projectId: "adapter-project-1",
          slug: "project-1",
          projectName: "p1",
          namespace: "Lea.P6a4584b313b8ddc4ba20e377",
          namespaceEditable: true,
          repoPath: "proofs/Lea/P6a4584b313b8ddc4ba20e377",
          hasRecordedProofs: true,
          exists: true
        });
      }
      return adapterFetch(url, options);
    }
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    targetText: "A theorem.",
    projectName: "stale-title"
  }, state);

  assert.equal(result.statusCode, 200);
  await waitFor(() => calls.some((c) => String(c.url).endsWith("/api/runs") && c.options?.method === "POST"));
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.ok(calls.some((c) => String(c.url).endsWith("/api/projects/by-slug/project-1/identity") && c.options?.method === "GET"));
  assert.ok(runCall.body.message.includes("Project display name: p1"));
  assert.ok(runCall.body.message.includes("Lean namespace: Lea.P6a4584b313b8ddc4ba20e377"));
  assert.ok(runCall.body.message.includes("Overleaf binding: project-1"));
  assert.ok(!runCall.body.message.includes("Lean namespace: Lea.Project1"));
  assert.equal(runCall.body.project_title, "p1");
  assert.equal(runCall.body.project_namespace, "Lea.P6a4584b313b8ddc4ba20e377");
});

test("formalize refreshes stale fallback identity and dependency paths after project rename", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  await writeLeaProjectProof(
    leaRepo,
    path.join("workspace", "proofs", "Lea", "P1", "compactness_criterion.lean"),
    "import Mathlib\n\nnamespace Lea.P1\n\ntheorem compactness_criterion : True := by\n  trivial\n\nend Lea.P1\n"
  );
  // The `uses=` dependency resolves through the ledger (4.3): the adapter
  // names the recorded file repo-relative; the REFRESHED identity namespace
  // (Lea.P1, not the stale Lea.Project1) must map it onto the right repo path.
  const adapterFetch = makeAdapterApiFetch(calls, {
    projectNamespace: "Lea.P1",
    targetStatus: {
      compactness_criterion: ledgerEntry("compactness_criterion", {
        module_name: "Lea.P1.compactness_criterion",
        content: "import Mathlib\n\nnamespace Lea.P1\n\ntheorem compactness_criterion : True := by\n  trivial\n\nend Lea.P1\n"
      })
    }
  });
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: async (url, options = {}) => {
      calls.push({ url: String(url), options, body: options.body ? JSON.parse(options.body) : null });
      if (String(url).endsWith("/api/projects/by-slug/project-1/identity") && options.method === "GET") {
        return jsonResponse(200, {
          projectId: "adapter-project-1",
          slug: "project-1",
          projectName: "p1",
          namespace: "Lea.P1",
          namespaceEditable: true,
          repoPath: "proofs/Lea/P1",
          hasRecordedProofs: true,
          exists: true
        });
      }
      return adapterFetch(url, options);
    }
  });
  state.projectIdentities = {
    "project-1": {
      slug: "project-1",
      projectName: "project-1",
      namespace: "Lea.Project1",
      exists: false
    }
  };
  state.jobs.old = {
    jobId: "old",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    recordedProofPath: path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean"),
    moduleName: "Lea.Project1.compactness_criterion",
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    projectName: "project-1",
    projectNamespace: "Lea.Project1",
    startedAt: "2026-07-09T01:48:17.300Z",
    finishedAt: "2026-07-09T01:48:51.752Z"
  };

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    targetText: "A consequence.",
    targetUses: ["compactness_criterion"]
  }, state);

  assert.equal(result.statusCode, 200);
  await waitFor(() => calls.some((c) => String(c.url).endsWith("/api/runs") && c.options?.method === "POST"));
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.ok(runCall.body.message.includes("Project display name: p1"));
  assert.ok(runCall.body.message.includes("Lean namespace: Lea.P1"));
  assert.ok(runCall.body.message.includes(path.join("workspace", "proofs", "Lea", "P1", "compactness_criterion.lean")));
  assert.ok(!runCall.body.message.includes("Lean namespace: Lea.Project1"));
  assert.ok(!runCall.body.message.includes(path.join("workspace", "proofs", "Lea", "Project1", "compactness_criterion.lean")));
  assert.equal(runCall.body.project_title, "p1");
  assert.equal(runCall.body.project_namespace, "Lea.P1");
});

test("stub on the /api backend records a checked sorry stub with a Lea session link", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "GeneratedStub.lean");
  const code = "namespace Lea.Project1\n\ntheorem generated_stub : True := by\n  sorry\n\nend Lea.Project1\n";
  await writeLeaProjectProof(leaRepo, proofPath, code);
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls, {
      doneStatus: "answered",
      sessionDetail: {
        project_namespace: "Lea.Project1",
        runs: [{ id: "api-run-1", input_tokens: 4, output_tokens: 6, cost_usd: 0.002 }],
        code_steps: [{
          id: "step-1",
          seq: 1,
          path: "GeneratedStub.lean",
          code,
          check_status: "ok",
          check_detail: "warning: declaration uses 'sorry'"
        }]
      }
    })
  });

  const result = await handleStub({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "generated_stub",
    targetText: "A generated stub theorem."
  }, state);

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "sorry_stub");
  assert.equal(result.body.leanStatement, "theorem generated_stub : True");
  assert.equal(result.body.leaSessionId, "sess-api-1");
  assert.equal(result.body.leaSessionUrl, "http://localhost:5173/?session=sess-api-1");
  assert.equal(state.jobs[result.body.jobId].recordedProofPath, proofPath);
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.match(runCall.body.message, /Create a Lean sorry stub/);
  assert.match(runCall.body.message, /Project display name: Overleaf Project/);
  assert.match(runCall.body.message, /Lean namespace: Lea\.Project1/);
  assert.match(runCall.body.message, /do not derive a namespace from the display name/);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "generated_stub", targetText: "A generated stub theorem." }]
  }, state);
  assert.equal(statuses.body.statuses["theorem:generated_stub"].status, "sorry_stub");
  assert.equal(statuses.body.statuses["theorem:generated_stub"].leanStatement, "theorem generated_stub : True");
});

test("formalize after a saved stub reuses the stub session and proof file", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "ReuseStub.lean");
  await writeLeaProjectProof(
    leaRepo,
    proofPath,
    "namespace Lea.Project1\n\ntheorem reuse_stub : True := by\n  sorry\n\nend Lea.Project1\n"
  );
  await writeLeaProjectMarkdown(leaRepo, "project-1", {
    theoremName: "reuse_stub",
    proofPath,
    moduleName: "Lea.Project1.ReuseStub"
  });
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls, {
      sessionDetail: { runs: [{ id: "api-run-1", input_tokens: 10, output_tokens: 5, cost_usd: 0.001 }], code_steps: [] }
    })
  });
  state.jobs.stub_job = {
    jobId: "stub_job",
    jobKey: "project-1:theorem:reuse_stub",
    status: "sorry_stub",
    finalStatus: "sorry_stub",
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetLabel: "reuse_stub",
    declarationName: "reuse_stub",
    recordedProofPath: proofPath,
    moduleName: "Lea.Project1.ReuseStub",
    leaRepoPath: leaRepo,
    leaSessionId: "sess-stub-1",
    leaUiBaseUrl: "http://localhost:5173",
    logPath: path.join(path.dirname(state.jobsPath), "stub.log"),
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:00:01.000Z"
  };
  await fs.writeFile(state.jobs.stub_job.logPath, "stub\n", "utf8");

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "reuse_stub",
    targetText: "A theorem."
  }, state);

  assert.equal(result.statusCode, 200);
  await waitFor(() => calls.some((c) => String(c.url).endsWith("/api/runs") && c.options?.method === "POST"));
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.equal(runCall.body.session_id, "sess-stub-1");
  assert.match(runCall.body.message, /Existing sorry stub to complete/);
  assert.equal(await fileExists(path.join(leaRepo, proofPath)), true);
});

test("re-formalize creates a new run in the target's existing Lea session", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls, {
      sessionId: "sess-existing",
      sessionDetail: {
        runs: [{ id: "api-run-1", input_tokens: 10, output_tokens: 5, cost_usd: 0.001 }],
        code_steps: []
      }
    })
  });
  state.jobs.previous = {
    jobId: "previous",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    overleafProjectId: "project-1",
    projectSlug: "project-1",
    projectNamespace: "Lea.Project1",
    leaSessionId: "sess-existing",
    formalizationId: "formalization-existing",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    targetText: "Every open cover has a finite subcover."
  }, state);

  assert.equal(result.statusCode, 200);
  await waitFor(() => calls.some((c) => String(c.url).endsWith("/api/runs") && c.options?.method === "POST"));
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.equal(runCall.body.session_id, "sess-existing");
  assert.equal(runCall.body.focus_formalization_id, "formalization-existing");
  assert.equal(runCall.body.new_formalization, undefined);
});

test("resolveProofOutcome trusts an adapter-verified run even with no local proof file", async () => {
  // The adapter defers project-markdown recording, so the companion often
  // cannot locate the proof on disk (localStatus === unformalized). A successful
  // run (exit.ok) must still resolve to `formalized` — this is the core fix.
  const job = { targetKind: "theorem", targetLabel: "t1", leaWorkspacePath: "/tmp/does-not-matter" };
  const outcome = await resolveProofOutcome({
    job,
    localStatus: { status: "unformalized" },
    exit: { ok: true }
  });

  assert.equal(outcome.jobStatus, "formalized");
  assert.equal(outcome.finalStatus, "formalized");
  assert.equal(outcome.effectiveStatus.status, "formalized");
  assert.equal(outcome.error, null);
  assert.equal(outcome.leanCheck, null);
});

test("resolveProofOutcome maps adapter disproof to disproved status", async () => {
  const outcome = await resolveProofOutcome({
    job: { targetKind: "theorem", targetLabel: "false_claim", leaWorkspacePath: "/tmp/does-not-matter" },
    localStatus: { status: "unformalized" },
    exit: {
      ok: true,
      doneStatus: "disproved",
      resultKind: "disproved",
      resultDetail: "DISPROVED"
    }
  });

  assert.equal(outcome.jobStatus, "disproved");
  assert.equal(outcome.finalStatus, "disproved");
  assert.equal(outcome.effectiveStatus.status, "disproved");
  assert.equal(outcome.resultKind, "disproved");
  assert.equal(outcome.error, null);
});

test("resolveProofOutcome keeps a verified run formalized when local evidence is also formalized", async () => {
  const job = { targetKind: "theorem", targetLabel: "t2", leaWorkspacePath: "/tmp/x" };
  const localStatus = { status: "formalized", leanStatement: "theorem t2 : True" };
  const outcome = await resolveProofOutcome({ job, localStatus, exit: { ok: true } });

  assert.equal(outcome.jobStatus, "formalized");
  assert.equal(outcome.finalStatus, "formalized");
  assert.equal(outcome.effectiveStatus, localStatus);
  // No absolutePath, so no local lean check is attempted.
  assert.equal(outcome.leanCheck, null);
});

test("resolveProofOutcome records a leftover sorry as a failed run with sorry_stub effective status", async () => {
  const job = { targetKind: "theorem", targetLabel: "t3", leaWorkspacePath: "/tmp/x" };
  const localStatus = { status: "sorry_stub", absolutePath: "/tmp/x/t3.lean" };
  const outcome = await resolveProofOutcome({ job, localStatus, exit: { ok: true } });

  assert.equal(outcome.jobStatus, "failed");
  assert.equal(outcome.finalStatus, "sorry_stub");
  assert.equal(outcome.effectiveStatus.status, "sorry_stub");
  assert.match(outcome.error, /sorry\/admit/);
});

test("resolveProofOutcome marks a failed run as failed and surfaces the run error", async () => {
  const job = { targetKind: "theorem", targetLabel: "t4", leaWorkspacePath: "/tmp/x" };
  const outcome = await resolveProofOutcome({
    job,
    localStatus: { status: "unformalized" },
    exit: { ok: false, error: "Lea run ended with status: failed" }
  });

  assert.equal(outcome.jobStatus, "failed");
  assert.equal(outcome.finalStatus, "unformalized");
  assert.equal(outcome.error, "Lea run ended with status: failed");
});

test("resolveProofOutcome promotes a needs_review run to formalized when local evidence independently confirms it", async () => {
  // Regression for the bug where a run that finished `needs_review` (bridge.py
  // groups it with proved/disproved as a completed, checked-artifact outcome --
  // NOT a crash/timeout) was collapsed into the same "failed" bucket as an
  // actual compile failure, because leaApiClient's SUCCESS_DONE_STATUS
  // deliberately excludes it so exit.ok is false. The pane showed `invalid` for
  // code that genuinely compiled.
  //
  // Once applyProofOutcomeToJob has independently confirmed the emitted file is
  // sorry-free and really compiles (recoverFormalizedStatusFromTargetPath, since
  // the agent itself skipped self-registering it), there is no principled reason
  // to hold it to a stricter bar than any other proof in this app -- promote it
  // all the way to `formalized`/valid, the same outcome a clean "proved" run
  // gets. The agent's own uncertainty is not evidence the artifact is wrong.
  const job = { targetKind: "theorem", targetLabel: "t5", leaWorkspacePath: "/tmp/x" };
  // The recovery path attaches its already-run, already-passing compile as
  // `leanCheck` -- promotion is gated on it (regex-derived "formalized" alone
  // must never promote; that's only half the "sorry-free + compiles" bar).
  // Attaching it here also keeps runLeanCheck (a real toolchain spawn) out of
  // the unit test.
  const leanCheck = { ok: true, exitCode: 0, stdout: "", stderr: "", message: "" };
  const localStatus = { status: "formalized", leanStatement: "theorem t5 : True", leanCheck };
  const outcome = await resolveProofOutcome({
    job,
    localStatus,
    exit: { ok: false, doneStatus: "needs_review", resultKind: "needs_review", resultDetail: "NEEDS_REVIEW" }
  });

  assert.equal(outcome.jobStatus, "formalized");
  assert.equal(outcome.finalStatus, "formalized");
  assert.equal(outcome.effectiveStatus, localStatus);
  assert.equal(outcome.resultKind, "proved");
  assert.equal(outcome.error, null);
  // reuses the attached check -- it must not pay for a second compile
  assert.equal(outcome.leanCheck, leanCheck);
});

test("resolveProofOutcome fails unconfirmed needs_review when the fresh compile fails", async () => {
  // `local.status === "formalized"` is a sorry/admit regex over the file, not
  // a compile. A needs_review run whose file is sorry-free but doesn't
  // actually compile must not promote on regex evidence alone.
  const job = { targetKind: "theorem", targetLabel: "t5b", leaWorkspacePath: "/tmp/x" };
  const failingCheck = { ok: false, exitCode: 1, stdout: "", stderr: "unknown identifier: foo", message: "" };
  const outcome = await resolveProofOutcome({
    job,
    localStatus: { status: "formalized", leanCheck: failingCheck },
    exit: { ok: false, doneStatus: "needs_review", resultKind: "needs_review", resultDetail: "NEEDS_REVIEW" }
  });

  assert.equal(outcome.jobStatus, "failed");
  assert.equal(outcome.finalStatus, "failed");
  assert.equal(outcome.effectiveStatus.status, "failed");
  assert.equal(outcome.resultKind, "needs_review");
  // the failing check is kept as the diagnostic explaining WHY
  assert.equal(outcome.leanCheck, failingCheck);
});

test("resolveProofOutcome fails unconfirmed needs_review when no compile evidence is possible", async () => {
  // Sorry-free file evidence but no absolutePath to check and no attached
  // recovery check: nothing verifies this actually compiles, so it must not
  // promote. (Unlike the exit.ok path, where the ADAPTER verified the run and
  // a local check is diagnostics-only, nothing upstream has verified a
  // needs_review result.)
  const job = { targetKind: "theorem", targetLabel: "t5c", leaWorkspacePath: "/tmp/x" };
  const outcome = await resolveProofOutcome({
    job,
    localStatus: { status: "formalized", leanStatement: "theorem t5c : True" },
    exit: { ok: false, doneStatus: "needs_review", resultKind: "needs_review", resultDetail: "NEEDS_REVIEW" }
  });

  assert.equal(outcome.jobStatus, "failed");
  assert.equal(outcome.finalStatus, "failed");
  assert.equal(outcome.effectiveStatus.status, "failed");
  assert.equal(outcome.resultKind, "needs_review");
  assert.equal(outcome.leanCheck, null);
});

// A fetch stub serving GET /api/sessions/{id} for the recovery tests below --
// the recovery resolves the run's real output file from its session's
// code_steps (the production path), not from any hand-crafted target shape.
function makeSessionDetailFetch(details) {
  return async (url) => {
    const match = String(url).match(/\/api\/sessions\/([^/]+)$/);
    const body = match ? details[decodeURIComponent(match[1])] : null;
    return {
      ok: Boolean(body),
      status: body ? 200 : 404,
      async text() { return JSON.stringify(body || { detail: "unknown session" }); }
    };
  };
}

function recoveryTarget(leaRepo, targetLabel) {
  // Deliberately the REAL production shape (buildLeaTarget): relativePath /
  // absolutePath point at the project MARKDOWN file, not any proof file, and
  // there is no moduleName. The first version of the recovery read those
  // fields -- and so sorry-scanned and lean-checked the markdown, returning
  // null on every real run -- while its tests hand-crafted a proof-file
  // relativePath that production never produces. These tests must not repeat
  // that: if the recovery ever reads target.relativePath again, they fail.
  const projectMarkdownPath = path.join(leaRepo, "workspace", "projects", "project-1.md");
  return {
    overleafProjectId: "project-1",
    projectId: "project-1",
    projectSlug: "project-1",
    targetKind: "theorem",
    targetLabel,
    theoremLabel: targetLabel,
    declarationName: targetLabel,
    projectMarkdownPath,
    relativePath: path.join("workspace", "projects", "project-1.md"),
    absolutePath: projectMarkdownPath,
    jobKey: `project-1:theorem:${targetLabel}`
  };
}

test("recoverFormalizedStatusFromTargetPath recovers a compiling proof via the session's code_steps (production target shape)", async () => {
  const restorePath = await installFakeLake(); // makes runLeanCheck pass
  try {
    const leaRepo = await makeLeaRepo();
    const relativePath = path.join("workspace", "proofs", "Lea", "Project1", "ok_thm.lean");
    await writeLeaProjectProof(leaRepo, relativePath, "theorem ok_thm : True := by\n  trivial\n");
    const job = {
      leaSessionId: "sess-r1",
      leaWorkspacePath: path.join(leaRepo, "workspace"),
      declarationNameHint: "ok_thm"
    };
    const state = await makeState({
      leaRepoPath: leaRepo,
      fetchImpl: makeSessionDetailFetch({
        "sess-r1": {
          project_namespace: "Lea.Project1",
          code_steps: [{ path: "ok_thm.lean", seq: 1, code: "theorem ok_thm : True := by\n  trivial\n" }]
        }
      })
    });

    const recovered = await recoverFormalizedStatusFromTargetPath({
      state,
      job,
      target: recoveryTarget(leaRepo, "ok_thm")
    });

    assert.ok(recovered, "expected the session-recorded proof to be recovered");
    assert.equal(recovered.status.status, "formalized");
    assert.equal(recovered.status.declarationName, "ok_thm");
    assert.equal(recovered.status.recordedProofPath, relativePath);
    assert.equal(recovered.leanCheck.ok, true);
  } finally {
    restorePath();
  }
});

test("recoverFormalizedStatusFromTargetPath returns null when the session's recorded file doesn't exist on disk", async () => {
  // The session says a step exists, but nothing was ever written at the
  // mapped repo path -- there's genuinely nothing to promote on; must not
  // fabricate a pass.
  const leaRepo = await makeLeaRepo();
  const job = { leaSessionId: "sess-r2", leaWorkspacePath: path.join(leaRepo, "workspace") };
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeSessionDetailFetch({
      "sess-r2": {
        project_namespace: "Lea.Project1",
        code_steps: [{ path: "never_written.lean", seq: 1, code: "theorem never_written : True := by\n  trivial\n" }]
      }
    })
  });

  const recovered = await recoverFormalizedStatusFromTargetPath({
    state,
    job,
    target: recoveryTarget(leaRepo, "never_written")
  });

  assert.equal(recovered, null);
});

test("recoverFormalizedStatusFromTargetPath returns null (not a false pass) when the file still has a sorry", async () => {
  const leaRepo = await makeLeaRepo();
  const relativePath = path.join("workspace", "proofs", "Lea", "Project1", "still_stub.lean");
  const code = "theorem still_stub : True := by\n  sorry\n";
  await writeLeaProjectProof(leaRepo, relativePath, code);
  const job = { leaSessionId: "sess-r3", leaWorkspacePath: path.join(leaRepo, "workspace") };
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeSessionDetailFetch({
      "sess-r3": {
        project_namespace: "Lea.Project1",
        code_steps: [{ path: "still_stub.lean", seq: 1, code }]
      }
    })
  });

  const recovered = await recoverFormalizedStatusFromTargetPath({
    state,
    job,
    target: recoveryTarget(leaRepo, "still_stub")
  });

  assert.equal(recovered, null);
});

test("recoverFormalizedStatusFromTargetPath returns null when the run has no session to consult", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });

  const recovered = await recoverFormalizedStatusFromTargetPath({
    state,
    job: { leaWorkspacePath: path.join(leaRepo, "workspace") },
    target: recoveryTarget(leaRepo, "sessionless")
  });

  assert.equal(recovered, null);
});

test("resolveProofOutcome records unconfirmed needs_review as failed when no local evidence was found", async () => {
  // This is the actual shape production hits, not a hypothetical: the
  // project-markdown index identifyLeaArtifact diffs against is populated by
  // the agent's own in-run tool calls, and it appears to skip that call when
  // it isn't confident enough to self-report "proved" -- so localStatus stays
  // "unformalized" for essentially every real needs_review run, even when the
  // emitted file may compile cleanly. Without checked artifact evidence, the
  // primary status should be failed/unconfirmed while resultKind preserves the
  // classifier metadata.
  const job = { targetKind: "theorem", targetLabel: "t6", leaWorkspacePath: "/tmp/x" };
  const outcome = await resolveProofOutcome({
    job,
    localStatus: { status: "unformalized" },
    exit: { ok: false, doneStatus: "needs_review", resultKind: "needs_review", error: "Lea run ended with status: needs_review" }
  });

  assert.equal(outcome.jobStatus, "failed");
  assert.equal(outcome.finalStatus, "failed");
  assert.equal(outcome.resultKind, "needs_review");
  assert.equal(outcome.effectiveStatus.status, "failed");
  assert.match(outcome.error, /could not confirm/);
});

test("formalize on the /api backend tags the theorem formalized when the run succeeds (regression)", async () => {
  // The /api adapter defers project-markdown recording, so the companion finds no
  // recorded artifact after the run. Before the fix the theorem was tagged
  // "failed" despite a verified proof; now the adapter's terminal `done: proved`
  // is authoritative and the tag must read "formalized".
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls)
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "t_api_success",
    targetText: "A theorem."
  }, state);

  await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");
  const job = state.jobs[result.body.jobId];
  assert.equal(job.exitCode, 0);
  assert.equal(job.error, undefined);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "t_api_success", targetText: "A theorem." }]
  }, state);
  assert.equal(statuses.statusCode, 200);
  assert.equal(statuses.body.statuses["theorem:t_api_success"].status, "formalized");
});

test("formalize on the /api backend tags the theorem failed when the run does not succeed", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch(calls, { doneStatus: "failed" })
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "t_api_failure",
    targetText: "A theorem."
  }, state);

  await waitFor(() => state.jobs[result.body.jobId]?.status === "failed");
  const job = state.jobs[result.body.jobId];
  assert.equal(job.exitCode, 1);

  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel: "t_api_failure", targetText: "A theorem." }]
  }, state);
  assert.equal(statuses.body.statuses["theorem:t_api_failure"].status, "failed");
});

// --- Lean-pane chat mirror -------------------------------------------------

const CHAT_TARGET = {
  overleafProjectId: "project-1",
  targetKind: "theorem",
  targetLabel: "compactness_criterion",
  latexLabel: "thm:compactness",
  sourceFile: "main.tex",
  sourceStartLine: 2,
  sourceEndLine: 5,
  sourceHash: "hash-current",
  naturalLanguageLatex: "Every open cover has a finite subcover.",
  leanDeclarationName: "compactness_criterion",
  status: "invalid"
};

function makeChatSessionDetail(overrides = {}) {
  return {
    status: overrides.status || "answered",
    messages: overrides.messages || [
      { id: "m2", role: "assistant", content: "It failed on the second goal.", kind: "assistant", seq: 2, created_at: "2026-01-01T00:00:02.000Z" },
      { id: "m1", role: "user", content: "Why did this fail?", kind: "user", seq: 1, created_at: "2026-01-01T00:00:01.000Z" },
      { id: "m3", role: "assistant", content: "calling write_file", kind: "tool_call", seq: 3, created_at: "2026-01-01T00:00:03.000Z" }
    ],
    runs: overrides.runs || [{ id: "api-run-1", status: "done", created_at: "2026-01-01T00:00:00.000Z" }],
    active_run: overrides.active_run ?? null
  };
}

function finishedChatJob(overrides = {}) {
  return {
    jobId: overrides.jobId || "chat-job",
    jobKey: "project-1:theorem:compactness_criterion",
    status: overrides.status || "failed",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    leaSessionId: overrides.leaSessionId || "sess-chat-1",
    leaUiBaseUrl: "http://localhost:5173",
    targetTextHash: overrides.targetTextHash ?? "hash-current",
    ...(overrides.formalizationInputHash
      ? { formalizationInputHash: overrides.formalizationInputHash }
      : {}),
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z"
  };
}

test("chat session load returns no-session when no job or association exists", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepoPath: leaRepo });

  const res = await handleChatSession({ target: CHAT_TARGET }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.status, "no-session");
  assert.equal(res.body.leaSessionId, null);
  assert.deepEqual(res.body.messages, []);
});

test("chat session load mirrors adapter messages, filtering tool narration and ordering by seq", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeAdapterApiFetch(calls, { sessionDetail: makeChatSessionDetail() })
  });
  state.jobs.chat = finishedChatJob();

  const res = await handleChatSession({ target: CHAT_TARGET }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.leaSessionId, "sess-chat-1");
  assert.equal(res.body.leaSessionUrl, "http://localhost:5173/?session=sess-chat-1");
  // tool_call message dropped; user/assistant kept in seq order
  assert.deepEqual(res.body.messages.map((m) => m.role), ["user", "assistant"]);
  assert.equal(res.body.messages[0].content, "Why did this fail?");
  assert.ok(calls.some((c) => String(c.url).includes("/api/sessions/sess-chat-1")));
});

test("chat session load surfaces adapter-unavailable while keeping the session link", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: async () => ({ ok: false, status: 502, async text() { return ""; } })
  });
  state.jobs.chat = finishedChatJob();

  const res = await handleChatSession({ target: CHAT_TARGET }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, false);
  assert.equal(res.body.error, "adapter_unavailable");
  assert.equal(res.body.leaSessionUrl, "http://localhost:5173/?session=sess-chat-1");
});

test("chat message starts a first-message session with the full context preamble", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });
  state.projectIdentities = {
    "project-1": {
      slug: "project-1",
      projectName: "p1",
      namespace: "Lea.P6a4584b313b8ddc4ba20e377"
    }
  };

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Why did this fail?" }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.leaSessionId, "sess-api-1");
  assert.equal(res.body.runId, "api-run-1");
  assert.equal(res.body.userMessage.content, "Why did this fail?");
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.equal(runCall.body.session_id, undefined);
  assert.match(runCall.body.message, /You are helping with this Overleaf item\./);
  assert.match(runCall.body.message, /Project display name: p1/);
  assert.match(runCall.body.message, /Lean namespace: Lea\.P6a4584b313b8ddc4ba20e377/);
  assert.match(runCall.body.message, /do not derive a namespace from the display name/);
  assert.match(runCall.body.message, /Natural-language statement:/);
  assert.match(runCall.body.message, /User request:\nWhy did this fail\?/);
  // association recorded for a target that had no prior job
  assert.equal(state.chatSessions["project-1:theorem:compactness_criterion"].leaSessionId, "sess-api-1");
  assert.equal(
    state.chatSessions["project-1:theorem:compactness_criterion"].formalizationInputHash,
    hashFormalizationInput({
      targetKind: "theorem",
      targetText: CHAT_TARGET.naturalLanguageLatex,
      targetUses: [],
      targetContext: ""
    })
  );
});

test("chat message continues an existing session with a minimal prompt", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });
  state.jobs.chat = finishedChatJob({ leaSessionId: "sess-existing", targetTextHash: "hash-current" });

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Please continue." }, state);

  assert.equal(res.statusCode, 200);
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.equal(runCall.body.session_id, "sess-existing");
  assert.equal(runCall.body.message, "Please continue.");
  assert.doesNotMatch(runCall.body.message, /You are helping/);
});

test("chat message prepends a stale note when the source hash drifted", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });
  state.jobs.chat = finishedChatJob({ leaSessionId: "sess-existing", targetTextHash: "hash-old" });

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Is this still right?" }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.stale, true);
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.match(runCall.body.message, /^Note: the Overleaf source changed after the known Lean artifact was generated\./);
  assert.match(runCall.body.message, /Updated item context:/);
  assert.match(runCall.body.message, /Source file: main\.tex:2-5/);
  assert.match(runCall.body.message, /User request:\nIs this still right\?$/);
});

test("chat message treats activation comment metadata drift as stale", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });
  state.jobs.chat = finishedChatJob({
    leaSessionId: "sess-existing",
    targetTextHash: hashTargetText(CHAT_TARGET.naturalLanguageLatex),
    formalizationInputHash: hashFormalizationInput({
      targetKind: "theorem",
      targetText: CHAT_TARGET.naturalLanguageLatex,
      targetUses: [],
      targetContext: ""
    })
  });

  const res = await handleChatMessage({
    target: {
      ...CHAT_TARGET,
      sourceHash: hashTargetText(CHAT_TARGET.naturalLanguageLatex),
      targetUses: ["finite_subcover"],
      targetContext: ""
    },
    message: "Can this use the new dependency?"
  }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.stale, true);
  const runCall = calls.find((c) => String(c.url).endsWith("/api/runs"));
  assert.match(runCall.body.message, /^Note: the Overleaf source changed after the known Lean artifact was generated\./);
  assert.match(runCall.body.message, /Declared dependencies: finite_subcover/);
});

test("chat message is blocked while a formalization run is active", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });
  state.jobs.active = {
    jobId: "active",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "in_progress",
    targetLabel: "compactness_criterion",
    leaSessionId: "sess-running",
    startedAt: "2026-01-01T00:00:00.000Z"
  };

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Hi" }, state);

  assert.equal(res.statusCode, 409);
  assert.equal(res.body.error, "run_in_progress");
});

test("chat message is blocked when the spend cap is reached", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    leaMaxSpendUsd: 0.01,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch([])
  });
  state.jobs.usage = makeUsageJob({ jobId: "usage", projectId: "project-1", inputTokens: 10, outputTokens: 5, costUsd: 0.5 });

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Hi" }, state);

  assert.equal(res.statusCode, 402);
  assert.equal(res.body.error, "max_spend_reached");
});

test("chat poll reshapes adapter session detail and reports the active run", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeAdapterApiFetch([], {
      sessionDetail: makeChatSessionDetail({ status: "running", active_run: { id: "api-run-1", status: "running" } })
    })
  });

  const res = await handleChatPoll({ sessionId: "sess-chat-1" }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.status, "running");
  assert.equal(res.body.activeRun.id, "api-run-1");
  assert.deepEqual(res.body.messages.map((m) => m.role), ["user", "assistant"]);
});

test("chat interrupt forwards the run id to the adapter", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeAdapterApiFetch(calls)
  });

  const res = await handleChatInterrupt({ runId: "api-run-1" }, state);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ok, true);
  assert.ok(calls.some((c) => String(c.url).includes("/api/runs/api-run-1/interrupt")));
});

test("chat message persists the association to disk when a path is configured", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls)
  });
  state.chatSessionsPath = path.join(path.dirname(state.jobsPath), "chatSessions.json");

  await handleChatMessage({ target: CHAT_TARGET, message: "Start here." }, state);

  const saved = JSON.parse(await fs.readFile(state.chatSessionsPath, "utf8"));
  assert.equal(saved["project-1:theorem:compactness_criterion"].leaSessionId, "sess-api-1");
  assert.equal(saved["project-1:theorem:compactness_criterion"].sourceHash, "hash-current");
});

// ── Export & GitHub sharing passthroughs (D34) ─────────────────────────────────

// A fake adapter for the by-slug share/export routes. `routes` maps a URL
// substring to { status, json } or { status, zip: bytes }; unmatched URLs 200 {}.
function makeShareFetch(calls, routes = {}) {
  return async (url, options = {}) => {
    const u = String(url);
    calls.push({ url: u, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    for (const [needle, spec] of Object.entries(routes)) {
      if (!u.includes(needle)) continue;
      if (spec.zip) {
        return {
          ok: true,
          status: 200,
          headers: { get: (h) => (h.toLowerCase() === "content-disposition" ? 'attachment; filename="doc-1.zip"' : "application/zip") },
          arrayBuffer: async () => spec.zip.buffer.slice(spec.zip.byteOffset, spec.zip.byteOffset + spec.zip.byteLength)
        };
      }
      return { ok: spec.status < 400, status: spec.status, text: async () => JSON.stringify(spec.json ?? {}) };
    }
    return { ok: true, status: 200, text: async () => "{}" };
  };
}

test("share status maps the adapter's by-slug payload and treats 404 as 'no project yet'", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, {
      "/share": { status: 200, json: { id: "p1", slug: "doc-1", remote_url: "https://github.com/me/doc", token_configured: true } }
    })
  });

  const res = await handleShareStatus({ overleafProjectId: "Doc 1" }, state);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, exists: true, remoteUrl: "https://github.com/me/doc", tokenConfigured: true });
  assert.ok(calls.some((c) => c.url.includes(`/api/projects/by-slug/${slugProjectId("Doc 1")}/share`)));

  // 404 (never-formalized document) is a calm "nothing to share yet", not an error.
  const emptyState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/share": { status: 404, json: { detail: "No Lea project exists for this document yet." } } })
  });
  const empty = await handleShareStatus({ overleafProjectId: "Doc 1" }, emptyState);
  assert.equal(empty.statusCode, 200);
  assert.deepEqual(empty.body, { ok: true, exists: false, remoteUrl: null, tokenConfigured: false });

  // Missing project id → 400 before any adapter call.
  const missing = await handleShareStatus({}, emptyState);
  assert.equal(missing.statusCode, 400);
  assert.equal(missing.body.error, "missing_project_id");
});

test("project graph passes the by-slug nodes/edges through and treats 404 as an empty graph", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const graph = {
    nodes: [{ key: "helper", kind: "lemma", status: "proved", uses: [] }],
    edges: [{ from: "helper", to: "base" }]
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, { "/graph": { status: 200, json: graph } })
  });

  const res = await handleProjectGraph({ overleafProjectId: "Doc 1" }, state);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, exists: true, nodes: graph.nodes, edges: graph.edges });
  assert.ok(calls.some((c) => c.url.includes(`/api/projects/by-slug/${slugProjectId("Doc 1")}/graph`)));

  // 404 (never-formalized document) → a calm empty graph, so the pane shows its empty state.
  const emptyState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/graph": { status: 404, json: { detail: "No Lea project exists for this document yet." } } })
  });
  const empty = await handleProjectGraph({ overleafProjectId: "Doc 1" }, emptyState);
  assert.equal(empty.statusCode, 200);
  assert.deepEqual(empty.body, { ok: true, exists: false, nodes: [], edges: [] });

  // A real adapter failure (not 404) surfaces as an error.
  const brokenState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/graph": { status: 502, json: { detail: "boom" } } })
  });
  const broken = await handleProjectGraph({ overleafProjectId: "Doc 1" }, brokenState);
  assert.equal(broken.statusCode, 502);
  assert.equal(broken.body.error, "graph_fetch_failed");

  // Missing project id → 400 before any adapter call.
  const noId = await handleProjectGraph({}, emptyState);
  assert.equal(noId.statusCode, 400);
  assert.equal(noId.body.error, "missing_project_id");
});

test("blueprint generate returns the added count + fresh graph, treating 404 as no-project", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const adapterBody = {
    added: 2, skipped: 1, warnings: [],
    graph: { nodes: [{ key: "a", kind: "lemma", status: "proved", uses: [] }], edges: [] },
  };
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, { "/blueprint/generate": { status: 200, json: adapterBody } }),
  });

  const res = await handleProjectBlueprintGenerate({ overleafProjectId: "Doc 1" }, state);
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.added, 2);
  assert.equal(res.body.skipped, 1);
  assert.deepEqual(res.body.nodes, adapterBody.graph.nodes);
  assert.ok(calls.some((c) => c.method === "POST" && c.url.includes(`/api/projects/by-slug/${slugProjectId("Doc 1")}/blueprint/generate`)));

  // 404 → benign "no project yet", nothing generated.
  const emptyState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/blueprint/generate": { status: 404, json: { detail: "No Lea project exists for this document yet." } } }),
  });
  const empty = await handleProjectBlueprintGenerate({ overleafProjectId: "Doc 1" }, emptyState);
  assert.equal(empty.statusCode, 200);
  assert.deepEqual(empty.body, { ok: true, exists: false, added: 0, skipped: 0, nodes: [], edges: [] });
});

test("set remote forwards to the by-slug route and surfaces the adapter's detail on rejection", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, {
      "/git/remote": { status: 200, json: { id: "p1", remote_url: "https://github.com/me/doc" } }
    })
  });

  const res = await handleShareSetRemote(
    { overleafProjectId: "Doc 1", remoteUrl: "https://github.com/me/doc/" }, state
  );
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.remoteUrl, "https://github.com/me/doc");
  const call = calls.find((c) => c.url.includes("/git/remote"));
  assert.equal(call.method, "PUT");
  assert.deepEqual(call.body, { remote_url: "https://github.com/me/doc/" });

  const badState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/git/remote": { status: 400, json: { detail: "Enter an https GitHub repo URL, e.g. https://github.com/you/repo" } } })
  });
  const bad = await handleShareSetRemote({ overleafProjectId: "Doc 1", remoteUrl: "ftp://x" }, badState);
  assert.equal(bad.statusCode, 400);
  assert.match(bad.body.message, /https GitHub repo URL/);
});

test("push forwards the adapter's user-facing detail, including the diverged-history hint", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], {
      "/git/push": { status: 200, json: { pushed: true, remote_url: "https://github.com/me/doc", detail: "Pushed." } }
    })
  });
  const res = await handleSharePush({ overleafProjectId: "Doc 1" }, state);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, remoteUrl: "https://github.com/me/doc", detail: "Pushed." });

  const divergedState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], {
      "/git/push": { status: 502, json: { detail: "Push failed: rejected\n\nThe GitHub repo has commits this project doesn't — you can ask Lea to reconcile them." } }
    })
  });
  const diverged = await handleSharePush({ overleafProjectId: "Doc 1" }, divergedState);
  assert.equal(diverged.statusCode, 502);
  assert.match(diverged.body.message, /reconcile/);
});

test("GitHub project import handlers preserve the Overleaf target identity through preview, confirm, and poll", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    const value = String(url);
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url: value, method: options.method || "GET", body });
    if (value.endsWith("/identity")) {
      return jsonResponse(200, {
        projectId: "p1",
        slug: "doc-1",
        projectName: "Document One",
        namespace: "Lea.DocumentOne",
        exists: true,
      });
    }
    if (value.endsWith("/github-imports/preview")) {
      return jsonResponse(200, { preview_id: "preview-1", plan: { counts: { add: 1 } } });
    }
    if (value.endsWith("/github-imports") && options.method === "POST") {
      return jsonResponse(202, { id: "import-1", status: "checking", counts: { checks: {} } });
    }
    if (value.endsWith("/github-imports/import-1")) {
      return jsonResponse(200, { id: "import-1", status: "complete", counts: { checks: { ok: 1 } } });
    }
    throw new Error(`unexpected fetch: ${value}`);
  };
  const state = await makeState({ leaRepoPath: leaRepo, fetchImpl });
  const target = {
    targetKind: "theorem",
    targetLabel: "stable-label",
    declarationName: "current_name",
    displayTitle: "Current theorem",
    statement: "A theorem.",
    sourceHash: "hash-1",
  };

  const preview = await handleGithubImportPreview({
    overleafProjectId: "doc-1",
    repositoryUrl: "https://github.com/owner/repo",
    targets: [target],
  }, state);
  assert.equal(preview.statusCode, 200);
  const previewCall = calls.find((call) => call.url.endsWith("/github-imports/preview"));
  assert.equal(previewCall.body.targets[0].origin_key, "doc-1:theorem:stable-label");
  assert.equal(previewCall.body.targets[0].declaration_name, "current_name");
  assert.equal(previewCall.body.namespace, "Lea.DocumentOne");

  const confirmed = await handleGithubImportConfirm({
    overleafProjectId: "doc-1",
    previewId: "preview-1",
  }, state);
  assert.equal(confirmed.statusCode, 202);
  const progress = await handleGithubImportStatus({
    overleafProjectId: "doc-1",
    importId: "import-1",
  }, state);
  assert.equal(progress.body.status, "complete");
});

test("project export streams the adapter zip through and softens a 404", async () => {
  const leaRepo = await makeLeaRepo();
  const zipBytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3]); // PK\x03\x04…
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, { "/export": { zip: zipBytes } })
  });

  const res = await handleProjectExport({ overleafProjectId: "Doc 1" }, state);
  assert.equal(res.statusCode, 200);
  assert.equal(res.zip.filename, "doc-1.zip");
  assert.deepEqual(Array.from(res.zip.bytes), Array.from(zipBytes));
  assert.ok(calls.some((c) => c.url.includes(`/api/projects/by-slug/${slugProjectId("Doc 1")}/export`)));

  const emptyState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch([], { "/export": { status: 404, json: { detail: "No Lea project exists for this document yet." } } })
  });
  const empty = await handleProjectExport({ overleafProjectId: "Doc 1" }, emptyState);
  assert.equal(empty.statusCode, 404);
  assert.match(empty.body.message, /formalize a theorem first/i);
});

test("github token update writes through to adapter settings and reports presence only", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(calls, {
      "/api/settings": { status: 200, json: { github_token: { configured: true, last4: "d3f4" } } }
    })
  });

  const res = await handleGithubTokenUpdate({ value: "ghp_abcd1234d3f4" }, state);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, githubTokenConfigured: true });
  const call = calls.find((c) => c.url.includes("/api/settings"));
  assert.equal(call.method, "PUT");
  assert.deepEqual(call.body, { github_token: { value: "ghp_abcd1234d3f4" } });
  // The synced adapter settings now drive the settings response's presence flag.
  assert.equal(state.adapterSettings?.github_token?.configured, true);

  const clearCalls = [];
  const clearState = await makeState({
    leaRepoPath: leaRepo,
    fetchImpl: makeShareFetch(clearCalls, {
      "/api/settings": { status: 200, json: { github_token: { configured: false, last4: null } } }
    })
  });
  const cleared = await handleGithubTokenUpdate({ clear: true }, clearState);
  assert.equal(cleared.statusCode, 200);
  assert.equal(cleared.body.githubTokenConfigured, false);
  assert.deepEqual(clearCalls.find((c) => c.url.includes("/api/settings")).body, { github_token: { clear: true } });

  // Neither a value nor clear → 400, and no adapter call.
  const noop = await handleGithubTokenUpdate({}, clearState);
  assert.equal(noop.statusCode, 400);
  assert.equal(noop.body.error, "missing_token");
});

// --- Self-repair Phase 1: post-run cascade for agent-driven changes --------
// docs/FEATURE-overleaf-self-repair.md Part 1: a chat-mirror run or a
// re-formalize that changes a recorded declaration must trigger the same
// cascade a manual edit does. Until this, those paths ran no cascade at all.

// Routes the full agent-run flow (POST /api/runs -> SSE events) PLUS the
// per-session detail/rebuild/lean-check calls the post-run cascade makes.
function makeCascadeRunFetch(calls, { runSessionId = "sess-api-1", sessionDetails = {}, rebuildResponses = {} } = {}) {
  return async (url, requestOptions = {}) => {
    const u = String(url);
    if (u.endsWith("/api/settings")) return jsonResponse(404, { detail: "not found" });
    const body = requestOptions.body ? JSON.parse(requestOptions.body) : null;
    calls.push({ url: u, options: requestOptions, body });
    if (u.endsWith("/api/runs") && requestOptions.method === "POST") {
      return jsonResponse(200, {
        run_id: "api-run-1",
        session_id: runSessionId,
        status: "running",
        project_namespace: "Lea.Project1"
      });
    }
    if (u.includes("/api/runs/") && u.endsWith("/events")) {
      return adapterSseResponse([
        { type: "status", data: { status: "tool_call", turn: 1 } },
        { type: "done", data: { status: "success" } }
      ]);
    }
    const match = u.match(/\/api\/sessions\/([^/]+)(?:\/(file|lean-check|rebuild))?$/);
    if (match) {
      const sessionId = decodeURIComponent(match[1]);
      const kind = match[2] || "detail";
      if (kind === "detail") return jsonResponse(200, sessionDetails[sessionId] || { code_steps: [] });
      if (kind === "rebuild") {
        return jsonResponse(200, rebuildResponses[sessionId] || { path: body?.path, status: "ok", detail: null });
      }
      if (kind === "lean-check") return jsonResponse(200, { path: body?.path, status: "ok", detail: null });
    }
    if (u.endsWith("/interrupt")) return jsonResponse(200, { status: "interrupting" });
    return jsonResponse(404, { detail: "not found" });
  };
}

function cascadeUpstreamJob(overrides = {}) {
  return {
    jobId: "job-upstream",
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    declarationName: "compactness_criterion",
    leaSessionId: "sess-chat-1",
    projectSlug: "project-1",
    projectNamespace: "Lea.Project1",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z",
    ...overrides
  };
}

function cascadeDependentJob(overrides = {}) {
  return {
    jobId: "job-dependent",
    jobKey: "project-1:theorem:compactness_corollary",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    leaSessionId: "sess-b",
    projectSlug: "project-1",
    projectNamespace: "Lea.Project1",
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z",
    ...overrides
  };
}

test("a chat run that renames the declaration cascades over dependents, attributes via=chat, and records lastRunImpact", async () => {
  const leaRepo = await makeLeaRepo();
  await writeLeaProjectProof(
    leaRepo,
    "workspace/proofs/Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeCascadeRunFetch(calls, {
      runSessionId: "sess-chat-1",
      sessionDetails: {
        "sess-chat-1": {
          project_namespace: "Lea.Project1",
          code_steps: [
            // seq 1: the pre-run recording (what the snapshot sees); seq 2: the
            // run's rename, already back-filled with a passing check verdict.
            { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion : True := by\n  sorry\n" },
            { path: "compactness_criterion.lean", seq: 2, code: "theorem compactness_thm : True := by\n  sorry\n", check_status: "ok" }
          ]
        }
      },
      rebuildResponses: {
        "sess-chat-1": { status: "ok", detail: null },
        "sess-b": { status: "error", detail: "unknown constant 'Lea.Project1.compactness_criterion'" }
      }
    })
  });
  state.jobs.upstream = cascadeUpstreamJob();
  state.jobs.dependent = cascadeDependentJob();

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Rename this theorem to compactness_thm." }, state);
  assert.equal(res.statusCode, 200);

  // The run's terminal continuation classifies + cascades in the background.
  await waitFor(() => Boolean(state.jobs.dependent.lastEditBreakage));

  const breakage = state.jobs.dependent.lastEditBreakage;
  assert.equal(breakage.via, "chat");
  assert.equal(breakage.classificationKind, "renamed");
  assert.equal(breakage.renamedTo, "compactness_thm");
  assert.equal(breakage.upstreamLabel, "compactness_criterion");
  assert.equal(state.jobs.dependent.lastEditCheckStatus, "error");
  // rename bookkeeping on the chat target's own linked job, same as manual edits
  assert.equal(state.jobs.upstream.declarationName, "compactness_thm");

  // lastRunImpact recorded on the chat-session record and surfaced by the poll APIs
  await waitFor(() => Boolean(state.chatSessions?.["project-1:theorem:compactness_criterion"]?.lastRunImpact));
  const impact = state.chatSessions["project-1:theorem:compactness_criterion"].lastRunImpact;
  assert.equal(impact.classification.kind, "renamed");
  assert.equal(impact.dependentsImpact.length, 1);
  assert.equal(impact.dependentsImpact[0].status, "invalid");
  assert.equal(impact.dependentsImpact[0].brokenByUpstream.via, "chat");

  const session = await handleChatSession({ target: CHAT_TARGET }, state);
  assert.equal(session.body.lastRunImpact.classification.kind, "renamed");
  const poll = await handleChatPoll({ sessionId: "sess-chat-1" }, state);
  assert.equal(poll.body.lastRunImpact.classification.kind, "renamed");

  // ...and a follow-up message supersedes the notice: the rename impact is
  // cleared before the new run starts. The second run changes nothing further
  // (declaration already renamed), so at most a no-broken-dependents impact
  // from its own continuation can replace it -- the rename notice is gone.
  await handleChatMessage({ target: CHAT_TARGET, message: "Thanks!" }, state);
  const remaining = state.chatSessions["project-1:theorem:compactness_criterion"].lastRunImpact;
  assert.ok(!remaining || remaining.classification.kind !== "renamed");
});

test("a chat run on a never-formalized item (no snapshot) runs no cascade and records no impact", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeCascadeRunFetch(calls, { runSessionId: "sess-new" })
  });

  const res = await handleChatMessage({ target: CHAT_TARGET, message: "Hello" }, state);
  assert.equal(res.statusCode, 200);
  // let the run's continuation settle
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(state.chatSessions["project-1:theorem:compactness_criterion"].lastRunImpact, undefined);
  // no rebuild was ever attempted
  assert.ok(!calls.some((c) => c.url.endsWith("/rebuild")));
});

test("a re-formalize whose outcome changes the signature cascades over dependents with via=formalize", async () => {
  const leaRepo = await makeLeaRepo();
  await writeLeaProjectProof(
    leaRepo,
    "workspace/proofs/Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeCascadeRunFetch(calls, {
      runSessionId: "sess-api-1",
      sessionDetails: {
        // the previous recording, snapshotted before the run
        "sess-prev": {
          project_namespace: "Lea.Project1",
          code_steps: [
            { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion : True := by\n  sorry\n" }
          ]
        },
        // the new run's session: same path, changed signature, checked ok
        "sess-api-1": {
          project_namespace: "Lea.Project1",
          code_steps: [
            { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion (h : True) : True := by\n  trivial\n", check_status: "ok" }
          ]
        }
      },
      rebuildResponses: {
        "sess-api-1": { status: "ok", detail: null },
        "sess-b": { status: "error", detail: "type mismatch after upstream signature change" }
      }
    })
  });
  state.jobs.upstream = cascadeUpstreamJob({ leaSessionId: "sess-prev" });
  state.jobs.dependent = cascadeDependentJob();

  const res = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    targetText: "Every open cover has a finite subcover."
  }, state);
  assert.equal(res.statusCode, 200);

  await waitFor(() => Boolean(state.jobs.dependent.lastEditBreakage));
  const breakage = state.jobs.dependent.lastEditBreakage;
  assert.equal(breakage.via, "formalize");
  assert.equal(breakage.classificationKind, "signature");
  assert.equal(breakage.upstreamLabel, "compactness_criterion");
  assert.equal(breakage.beforeHeader, "theorem compactness_criterion : True");
  assert.equal(breakage.afterHeader, "theorem compactness_criterion (h : True) : True");
  assert.equal(state.jobs.dependent.lastEditCheckStatus, "error");
});

// --- Stale-offer reconciliation, chat side (PLAN-self-repair-stale-offers Fix 2)

test("chat lastRunImpact is annotated with each dependent's LIVE state at serve time, without rewriting the stored record", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeCascadeRunFetch(calls, {
      sessionDetails: { "sess-chat-1": { project_namespace: "Lea.Project1", code_steps: [], messages: [] } }
    })
  });
  state.jobs.upstream = cascadeUpstreamJob();
  state.jobs.dependent = cascadeDependentJob({
    lastEditCheckStatus: "error",
    lastEditBreakage: { upstreamLabel: "compactness_criterion", classificationKind: "renamed", via: "chat", editedAt: "t" }
  });
  // A stored impact record, as a completed chat run's continuation left it.
  state.chatSessions = {
    "project-1:theorem:compactness_criterion": {
      leaSessionId: "sess-chat-1",
      createdAt: "t",
      updatedAt: "t",
      sourceHash: null,
      lastRunImpact: {
        classification: { kind: "renamed", from: "compactness_criterion", to: "compactness_thm" },
        targetLabel: "compactness_criterion",
        finishedAt: "t",
        dependentsImpact: [
          { targetLabel: "compactness_corollary", status: "invalid", attributed: true, busy: false, brokenByUpstream: { targetLabel: "compactness_criterion", renamed: true, via: "chat" } }
        ]
      }
    }
  };

  // While the dependent's job truth says broken: stillBroken.
  const whileBroken = await handleChatSession({ target: CHAT_TARGET }, state);
  assert.equal(whileBroken.body.lastRunImpact.dependentsImpact[0].stillBroken, true);
  assert.equal(whileBroken.body.lastRunImpact.dependentsImpact[0].nowRepairing, false);

  // The dependent gets fixed through ANY path (repair, manual edit, batch):
  // the next poll reflects it while the stored record is untouched.
  state.jobs.dependent.lastEditCheckStatus = "ok";
  delete state.jobs.dependent.lastEditBreakage;

  const afterFix = await handleChatSession({ target: CHAT_TARGET }, state);
  assert.equal(afterFix.body.lastRunImpact.dependentsImpact[0].stillBroken, false);
  // history preserved: the record still says what the run broke at the time
  assert.equal(afterFix.body.lastRunImpact.dependentsImpact[0].brokenByUpstream.renamed, true);
  const stored = state.chatSessions["project-1:theorem:compactness_criterion"].lastRunImpact;
  assert.equal(stored.dependentsImpact[0].stillBroken, undefined);

  // the sessionId-only poll path resolves the project slug from the record key
  const poll = await handleChatPoll({ sessionId: "sess-chat-1" }, state);
  assert.equal(poll.body.lastRunImpact.dependentsImpact[0].stillBroken, false);

  // a live run on the dependent reads as nowRepairing, not stillBroken
  state.jobs.dependent.lastEditCheckStatus = "error";
  state.jobs.dependent.lastEditBreakage = { upstreamLabel: "compactness_criterion", classificationKind: "renamed", via: "chat", editedAt: "t" };
  state.jobs.live = { jobId: "live", jobKey: "project-1:theorem:compactness_corollary", status: "in_progress", mode: "repair", leaSessionId: "sess-b" };
  const whileRepairing = await handleChatSession({ target: CHAT_TARGET }, state);
  assert.equal(whileRepairing.body.lastRunImpact.dependentsImpact[0].nowRepairing, true);
  assert.equal(whileRepairing.body.lastRunImpact.dependentsImpact[0].stillBroken, false);
});

test("adapter-side mid-run spend cap maps to finalStatus max_spend without an interrupt", async () => {
  // PLAN-system-hardening 0.1: the bridge stops a run at the cap and labels the
  // terminal done frame result_kind="max_spend" (status stays "cancelled").
  // The companion must adopt its usual max-spend bookkeeping without trying to
  // interrupt the already-terminal run.
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeLeaApiFetch(calls, {
      eventFrames: [
        { type: "status", status: "max_spend", turn: 1 },
        {
          type: "done",
          status: "cancelled",
          result_kind: "max_spend",
          result_detail: "Max spend limit reached; the run was stopped at a turn boundary."
        }
      ]
    })
  });

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "adapter_cap_test",
    targetText: "A theorem."
  }, state);

  assert.equal(result.statusCode, 200);
  await waitFor(() => state.jobs[result.body.jobId]?.finalStatus === "max_spend");

  const job = state.jobs[result.body.jobId];
  assert.equal(job.status, "failed");
  assert.equal(job.error, "Max spend limit reached. Lea run was cancelled.");
  // The run is already terminal adapter-side — no interrupt round-trip.
  assert.ok(!calls.some((call) => String(call.url).endsWith("/interrupt")));
});

test("formalize lifecycle publishes jobs-changed push events (PLAN 3.1)", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([])
  });
  const { createEventBus } = await import("../companion/eventBus.mjs");
  state.eventBus = createEventBus();
  const seen = [];
  state.eventBus.subscribe((event) => seen.push(event.type));

  const result = await handleFormalize({
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "push_events_test",
    targetText: "A theorem."
  }, state);
  assert.equal(result.statusCode, 200);
  await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");

  const jobsChanged = seen.filter((type) => type === "jobs-changed");
  assert.ok(jobsChanged.length >= 2,
    `expected registration + terminal jobs-changed events, saw ${seen.join(", ")}`);
});

test("formalize resolves the artifact from the adapter index, no agent markdown needed (PLAN 4.2)", async () => {
  const leaRepo = await makeLeaRepo();
  // A located artifact makes resolveProofOutcome run its diagnostics
  // lean-check — neutralize the spawn like every artifact-locating test.
  const restorePath = await installFakeLake();
  try {
    const proofPath = path.join("workspace", "proofs", "Lea", "Project1", "adapter_index_test.lean");
    // The agent wrote a valid proof file but never self-registered a markdown
    // entry — the adapter's structured index is the only identification source.
    await writeLeaProjectProof(
      leaRepo,
      proofPath,
      "import Mathlib\n\ntheorem adapter_index_test : True := by\n  trivial\n"
    );
    const state = await makeState({
      leaRepoPath: leaRepo,
      env: { OPENAI_API_KEY: "test-key" },
      fetchImpl: makeAdapterApiFetch([], {
        projectNamespace: "Lea.Project1",
        artifacts: [{
          run_id: "api-run-1",
          declaration_name: "adapter_index_test",
          kind: "proof",
          path: "adapter_index_test.lean",
          module_name: "Lea.Project1.adapter_index_test"
        }]
      })
    });

    const result = await handleFormalize({
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "adapter_index_test",
      targetText: "A theorem."
    }, state);
    assert.equal(result.statusCode, 200);
    await waitFor(() => state.jobs[result.body.jobId]?.status === "formalized");

    const job = state.jobs[result.body.jobId];
    assert.equal(job.declarationName, "adapter_index_test");
    assert.equal(job.recordedProofPath, "workspace/proofs/Lea/Project1/adapter_index_test.lean");
    assert.equal(job.moduleName, "Lea.Project1.adapter_index_test");

    // The registry markdown got a consistency entry (until 4.3 demotes it to a
    // view, markdown-dependent readers still need one).
    const markdown = await fs.readFile(
      buildLeaProjectMarkdownPath({ leaRepoPath: leaRepo, overleafProjectId: "project-1" }),
      "utf8"
    );
    assert.match(markdown, /lea:theorem name="adapter_index_test"/);

    const log = await fs.readFile(job.logPath, "utf8");
    assert.match(log, /Artifact resolved from the adapter index/);
  } finally {
    restorePath();
  }
});

// --- ledger status engine (PLAN 4.4 — the only engine since the flip) --------
// Two sources only: the adapter's per-declaration ledger evidence + the
// companion's job overlay. No markdown parsing, no direct-FS regex probes.

function ledgerEntry(name, overrides = {}) {
  return {
    declaration_name: name,
    recorded: true,
    path: `${name}.lean`,
    module_name: `Lea.Project1.${name}`,
    kind: "proof",
    exists: true,
    declaration_present: true,
    has_sorry: false,
    check_status: "ok",
    check_detail: null,
    check_author: "agent",
    content: `import Mathlib\n\ntheorem ${name} : True := by\n  trivial\n`,
    ...overrides
  };
}

async function makeLedgerState(targetStatus, overrides = {}) {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepoPath: leaRepo,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl: makeAdapterApiFetch([], { targetStatus }),
    ...overrides
  });
  return { state, leaRepo };
}

async function ledgerStatusFor(state, targetLabel) {
  const statuses = await handleGetStatuses({
    overleafProjectId: "project-1",
    targets: [{ targetKind: "theorem", targetLabel, targetText: "A theorem." }]
  }, state);
  assert.equal(statuses.statusCode, 200);
  return statuses.body.statuses[`theorem:${targetLabel}`];
}

test("ledger engine: recorded + clean check reads formalized with the lean statement", async () => {
  const { state } = await makeLedgerState({ ledger_ok: ledgerEntry("ledger_ok") });
  const info = await ledgerStatusFor(state, "ledger_ok");
  assert.equal(info.status, "formalized");
  assert.equal(info.resultKind, "proved");
  assert.equal(info.recordedProofPath, "workspace/proofs/Lea/Project1/ledger_ok.lean");
  assert.match(info.leanStatement || "", /theorem ledger_ok/);
});

test("ledger engine: a sorry in the recorded file reads sorry_stub", async () => {
  const { state } = await makeLedgerState({
    ledger_sorry: ledgerEntry("ledger_sorry", {
      has_sorry: true,
      content: "theorem ledger_sorry : True := by\n  sorry\n"
    })
  });
  const info = await ledgerStatusFor(state, "ledger_sorry");
  assert.equal(info.status, "sorry_stub");
});

test("ledger engine: an error check verdict on the recorded file reads broken", async () => {
  const { state } = await makeLedgerState({
    ledger_broken: ledgerEntry("ledger_broken", { check_status: "error", check_detail: "type mismatch" })
  });
  const info = await ledgerStatusFor(state, "ledger_broken");
  assert.equal(info.status, "failed");
  assert.equal(info.effectiveStatus, "unformalized");
  assert.match(info.message || "", /type mismatch/);
});

test("ledger engine: index-known-gone artifact ignores a stale formalized job and reads failed from the overlay", async () => {
  const { state, leaRepo } = await makeLedgerState({
    ledger_retired: ledgerEntry("ledger_retired", { exists: false, has_sorry: null, content: null })
  });
  const logPath = path.join(path.dirname(state.jobsPath), "ledger-retired.log");
  await fs.writeFile(logPath, "old formalize\n", "utf8");
  state.jobs.stale_formalized = {
    jobId: "stale_formalized", jobKey: "project-1:theorem:ledger_retired", status: "formalized",
    targetKind: "theorem", targetLabel: "ledger_retired", declarationName: "ledger_retired",
    overleafProjectId: "project-1", projectSlug: "project-1", logPath,
    leaRepoPath: leaRepo, startedAt: "2026-01-01T00:00:00.000Z", finishedAt: "2026-01-01T00:00:01.000Z"
  };
  state.jobs.newer_failed = {
    jobId: "newer_failed", jobKey: "project-1:theorem:ledger_retired", status: "failed",
    targetKind: "theorem", targetLabel: "ledger_retired", declarationName: "ledger_retired",
    overleafProjectId: "project-1", projectSlug: "project-1", logPath, error: "retry failed",
    leaRepoPath: leaRepo, startedAt: "2026-01-02T00:00:00.000Z", finishedAt: "2026-01-02T00:00:01.000Z"
  };
  const info = await ledgerStatusFor(state, "ledger_retired");
  assert.equal(info.status, "failed");
  assert.match(info.logTail || "", /old formalize/);
});

test("ledger engine: a recorded artifact whose file is gone reads unformalized when no job remains", async () => {
  const { state } = await makeLedgerState({
    ledger_gone: ledgerEntry("ledger_gone", { exists: false, has_sorry: null, content: null })
  });
  const info = await ledgerStatusFor(state, "ledger_gone");
  assert.equal(info.status, "unformalized");
});

test("ledger engine: unrecorded declaration with no jobs reads unformalized; a formalized job wins index ignorance", async () => {
  const { state, leaRepo } = await makeLedgerState({});
  const empty = await ledgerStatusFor(state, "ledger_unknown");
  assert.equal(empty.status, "unformalized");

  state.jobs.pre_index = {
    jobId: "pre_index", jobKey: "project-1:theorem:ledger_preindex", status: "formalized",
    targetKind: "theorem", targetLabel: "ledger_preindex", declarationName: "ledger_preindex",
    overleafProjectId: "project-1", projectSlug: "project-1",
    leaRepoPath: leaRepo, startedAt: "2026-01-01T00:00:00.000Z", finishedAt: "2026-01-01T00:00:01.000Z"
  };
  const preIndex = await ledgerStatusFor(state, "ledger_preindex");
  assert.equal(preIndex.status, "formalized",
    "an index that never saw the declaration defers to the job record");
});
