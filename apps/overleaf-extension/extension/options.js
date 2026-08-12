const DEFAULT_COMPANION_URL = "http://127.0.0.1:31245";
const DEFAULT_LEA_TEX_MIRROR_ENABLED = true;
const MODEL_FAMILY_LABELS = {
  openai: "OpenAI",
  google: "Google AI",
  anthropic: "Anthropic"
};
// Placeholder only, used before the first successful companion fetch. The
// adapter's LiteLLM catalog is authoritative; the shared package supplies the
// offline featured fallback. Keep the default in sync with content.js (AUDIT L9).
const DEFAULT_LEA_MODEL = "o4-mini";
const DEFAULT_MODEL_OPTIONS = [
  { value: DEFAULT_LEA_MODEL, label: DEFAULT_LEA_MODEL, family: "openai" }
];

const form = document.querySelector("#settings-form");
const companionUrlInput = document.querySelector("#companion-url");
const leaRepoPathInput = document.querySelector("#lea-repo-path");
const leaApiBaseUrlInput = document.querySelector("#lea-api-base-url");
const leaModelInput = document.querySelector("#lea-model");
const modelCatalogStatus = document.querySelector("#model-catalog-status");
const modelRequirementsContainer = document.querySelector("#model-requirements");
const leaMaxTurnsInput = document.querySelector("#lea-max-turns");
const leaTexMirrorInput = document.querySelector("#lea-tex-mirror");
const providerStatusList = document.querySelector("#provider-key-status");
const providerKeyInputs = {
  openai: document.querySelector("#openai-api-key"),
  google: document.querySelector("#gemini-api-key"),
  anthropic: document.querySelector("#anthropic-api-key")
};
const loadCompanionSettingsButton = document.querySelector("#load-companion-settings");
const statusEl = document.querySelector("#status");
let latestModelOptions = DEFAULT_MODEL_OPTIONS;
let latestModelCatalog = DEFAULT_MODEL_OPTIONS;
let latestProviderKeys = {};
let latestApiKeys = {};
let latestModelRequirements = null;

chrome.storage.sync.get(
  {
    companionUrl: DEFAULT_COMPANION_URL,
    leaRepoPath: "",
    leaApiBaseUrl: "http://127.0.0.1:8001",
    leaModel: DEFAULT_LEA_MODEL,
    leaMaxTurns: 20,
    leaTexMirrorEnabled: DEFAULT_LEA_TEX_MIRROR_ENABLED
  },
  (settings) => {
    companionUrlInput.value = settings.companionUrl;
    leaRepoPathInput.value = settings.leaRepoPath;
    leaApiBaseUrlInput.value = settings.leaApiBaseUrl;
    renderModelOptions(DEFAULT_MODEL_OPTIONS, DEFAULT_MODEL_OPTIONS, settings.leaModel || DEFAULT_LEA_MODEL);
    renderProviderKeyStatus(latestProviderKeys);
    leaMaxTurnsInput.value = settings.leaMaxTurns;
    if (leaTexMirrorInput) leaTexMirrorInput.checked = settings.leaTexMirrorEnabled !== false;
    loadCompanionSettings({ silent: true });
  }
);

loadCompanionSettingsButton.addEventListener("click", () => {
  loadCompanionSettings({ silent: false });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusEl.textContent = "Validating Lea settings...";

  const companionUrl = companionUrlInput.value.trim().replace(/\/+$/, "");
  const leaRepoPath = leaRepoPathInput.value.trim();
  const leaApiBaseUrl = leaApiBaseUrlInput.value.trim().replace(/\/+$/, "");
  const leaModel = leaModelInput.value.trim() || DEFAULT_LEA_MODEL;
  const leaMaxTurns = Number.parseInt(leaMaxTurnsInput.value, 10) || 20;
  const leaTexMirrorEnabled = leaTexMirrorInput ? leaTexMirrorInput.checked : DEFAULT_LEA_TEX_MIRROR_ENABLED;
  const leaProviderApiKeys = collectProviderApiKeyPatch();
  try {
    const leaResponse = await fetch(`${companionUrl}/settings/lea`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        leaRepoPath,
        leaApiBaseUrl,
        leaModel,
        leaMaxTurns,
        leaTexMirrorEnabled,
        leaProviderApiKeys,
        leaApiKeys: collectDynamicApiKeyPatch()
      })
    });
    const leaPayload = await leaResponse.json().catch(() => ({}));
    if (!leaResponse.ok) {
      throw new Error(leaPayload.message || `Companion returned HTTP ${leaResponse.status}.`);
    }

    await chrome.storage.sync.set({
      companionUrl,
      leaRepoPath: leaPayload.leaRepoPath,
      leaApiBaseUrl: leaPayload.leaApiBaseUrl,
      leaModel: leaPayload.leaModel,
      leaMaxTurns: leaPayload.leaMaxTurns,
      leaTexMirrorEnabled: leaPayload.leaTexMirrorEnabled
    });
    latestProviderKeys = leaPayload.leaProviderKeys || latestProviderKeys;
    latestApiKeys = leaPayload.leaApiKeys || latestApiKeys;
    renderProviderKeyStatus(latestProviderKeys);
    clearProviderKeyInputs();
    clearDynamicApiKeyInputs();
    await loadModelRequirements(leaPayload.leaModel || leaModel);
    statusEl.textContent = "Settings saved.";
  } catch (error) {
    statusEl.textContent = error instanceof Error ? error.message : String(error);
  }
});

async function loadCompanionSettings({ silent }) {
  const companionUrl = companionUrlInput.value.trim().replace(/\/+$/, "") || DEFAULT_COMPANION_URL;

  try {
    const response = await fetch(`${companionUrl}/settings`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    }

    companionUrlInput.value = companionUrl;
    leaRepoPathInput.value = payload.leaRepoPath || leaRepoPathInput.value;
    leaApiBaseUrlInput.value = payload.leaApiBaseUrl || leaApiBaseUrlInput.value || "http://127.0.0.1:8001";
    latestModelOptions = payload.leaModelOptions || DEFAULT_MODEL_OPTIONS;
    latestProviderKeys = payload.leaProviderKeys || {};
    latestApiKeys = payload.leaApiKeys || {};
    renderProviderKeyStatus(latestProviderKeys);
    const catalogPayload = await loadModelCatalog(companionUrl);
    latestModelCatalog = catalogPayload.models;
    renderModelOptions(
      latestModelCatalog,
      latestModelOptions,
      payload.leaModel || leaModelInput.value || DEFAULT_LEA_MODEL
    );
    await loadModelRequirements(payload.leaModel || leaModelInput.value || DEFAULT_LEA_MODEL);
    leaMaxTurnsInput.value = payload.leaMaxTurns || leaMaxTurnsInput.value || 20;
    if (leaTexMirrorInput) leaTexMirrorInput.checked = payload.leaTexMirrorEnabled !== false;

    await chrome.storage.sync.set({
      companionUrl,
      leaRepoPath: leaRepoPathInput.value,
      leaApiBaseUrl: leaApiBaseUrlInput.value,
      leaModel: leaModelInput.value,
      leaMaxTurns: Number.parseInt(leaMaxTurnsInput.value, 10) || 20,
      leaTexMirrorEnabled: leaTexMirrorInput ? leaTexMirrorInput.checked : DEFAULT_LEA_TEX_MIRROR_ENABLED
    });

    if (!silent) {
      statusEl.textContent = "Loaded settings from companion.";
    }
  } catch (error) {
    if (!silent) {
      statusEl.textContent = error instanceof Error ? error.message : String(error);
    }
  }
}

for (const input of Object.values(providerKeyInputs)) {
  input.addEventListener("input", () => {
    updateRequirementSummary(latestModelRequirements);
  });
}

function renderModelOptions(catalog, featured, selectedModel) {
  globalThis.LeaModelPicker.createModelPicker({
    root: leaModelInput,
    value: selectedModel,
    catalog,
    featured,
    onChange: (model) => {
      void loadModelRequirements(model);
    }
  });
}

async function loadModelCatalog(companionUrl) {
  try {
    const response = await fetch(`${companionUrl}/settings/models`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
    const models = Array.isArray(payload.models) && payload.models.length > 0
      ? payload.models
      : latestModelOptions;
    if (modelCatalogStatus) {
      modelCatalogStatus.textContent = payload.degraded
        ? "The adapter catalog is unavailable; showing the offline fallback and current model."
        : `${models.length.toLocaleString()} models available. Search by model ID or provider.`;
    }
    return { models, degraded: Boolean(payload.degraded) };
  } catch {
    if (modelCatalogStatus) {
      modelCatalogStatus.textContent = "The adapter catalog is unavailable; showing the offline fallback.";
    }
    return { models: latestModelOptions, degraded: true };
  }
}

async function loadModelRequirements(model) {
  const companionUrl = companionUrlInput.value.trim().replace(/\/+$/, "") || DEFAULT_COMPANION_URL;
  try {
    const response = await fetch(
      `${companionUrl}/settings/models/requirements?model=${encodeURIComponent(model)}`
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
    latestModelRequirements = payload;
    renderModelRequirements(payload);
  } catch {
    latestModelRequirements = null;
    renderModelRequirements(null);
  }
}

function staticProviderInputForEnv(env) {
  if (env === "OPENAI_API_KEY") return providerKeyInputs.openai;
  if (env === "GOOGLE_API_KEY" || env === "GEMINI_API_KEY") return providerKeyInputs.google;
  if (env === "ANTHROPIC_API_KEY" || env === "ANTHROPIC_AUTH_TOKEN") return providerKeyInputs.anthropic;
  return null;
}

function requirementConfigured(requirement) {
  if (requirement?.configured || latestApiKeys?.[requirement?.env]?.configured) return true;
  const staticInput = staticProviderInputForEnv(requirement?.env);
  if (staticInput?.value.trim()) return true;
  return [...modelRequirementsContainer?.querySelectorAll("input[data-env]") || []]
    .some((input) => input.dataset.env === requirement?.env && Boolean(input.value.trim()));
}

function updateRequirementSummary(requirements) {
  const note = modelRequirementsContainer?.querySelector(".lea-model-requirement-note");
  if (!note || !requirements) return;
  const required = Array.isArray(requirements.required_keys) ? requirements.required_keys : [];
  const satisfied = required.length === 0 || required.some(requirementConfigured);
  note.dataset.satisfied = satisfied ? "true" : "false";
  if (required.length === 0) {
    note.textContent = requirements.degraded
      ? "Provider requirements are unavailable while the adapter is offline."
      : "This model does not require a single API-key credential.";
  } else if (satisfied) {
    note.textContent = `${requirements.provider || "Model"} credentials are configured.`;
  } else {
    note.textContent = `Add one of the required credentials: ${required.map((key) => key.env).join(" or ")}.`;
  }
}

function renderModelRequirements(requirements) {
  if (!modelRequirementsContainer) return;
  modelRequirementsContainer.replaceChildren();
  if (!requirements) {
    const unavailable = document.createElement("p");
    unavailable.className = "lea-model-requirement-note";
    unavailable.textContent = "Model credential requirements are currently unavailable.";
    modelRequirementsContainer.appendChild(unavailable);
    return;
  }
  const note = document.createElement("p");
  note.className = "lea-model-requirement-note";
  modelRequirementsContainer.appendChild(note);
  for (const requirement of requirements.required_keys || []) {
    if (staticProviderInputForEnv(requirement.env)) continue;
    const label = document.createElement("label");
    label.className = "lea-model-requirement-field";
    label.textContent = requirement.label || requirement.env;
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.dataset.env = requirement.env;
    input.placeholder = requirement.configured || latestApiKeys?.[requirement.env]?.configured
      ? "Configured — leave blank to keep"
      : requirement.env;
    input.addEventListener("input", () => updateRequirementSummary(requirements));
    label.appendChild(input);
    modelRequirementsContainer.appendChild(label);
  }
  updateRequirementSummary(requirements);
}

function collectDynamicApiKeyPatch() {
  const patch = {};
  for (const input of modelRequirementsContainer?.querySelectorAll("input[data-env]") || []) {
    const value = input.value.trim();
    if (value) patch[input.dataset.env] = value;
  }
  return patch;
}

function clearDynamicApiKeyInputs() {
  for (const input of modelRequirementsContainer?.querySelectorAll("input[data-env]") || []) {
    input.value = "";
  }
}

function collectProviderApiKeyPatch() {
  const patch = {};
  for (const [family, input] of Object.entries(providerKeyInputs)) {
    const value = input.value.trim();
    if (value) patch[family] = value;
  }
  return patch;
}

function clearProviderKeyInputs() {
  for (const input of Object.values(providerKeyInputs)) {
    input.value = "";
  }
}

// Paste-into-preamble alternative to \usepackage{lea-tags}, for users who
// don't want to add a second project file. Must define the same commands and
// environments as extension/assets/lea-tags.sty (kept in sync by hand).
const LEA_TAGS_PREAMBLE_SNIPPET = [
  "\\RequirePackage{xparse}",
  "\\RequirePackage{listings}",
  "\\NewDocumentCommand{\\lea}{m g}{\\IfValueT{#2}{#2}}",
  "\\NewDocumentCommand{\\leatheorem}{m g}{\\IfValueT{#2}{#2}}",
  "\\NewDocumentCommand{\\lealemma}{m g}{\\IfValueT{#2}{#2}}",
  "\\NewDocumentCommand{\\leaproposition}{m g}{\\IfValueT{#2}{#2}}",
  "\\NewDocumentCommand{\\leacorollary}{m g}{\\IfValueT{#2}{#2}}",
  "\\NewDocumentCommand{\\leadefinition}{m g}{\\IfValueT{#2}{#2}}",
  "\\lstnewenvironment{leacode}[1]{\\lstset{basicstyle=\\ttfamily\\small,breaklines=true,columns=fullflexible,keepspaces=true,showstringspaces=false,frame=none}}{}"
].join("\n");

const leaTagsCopySnippetButton = document.querySelector("#lea-tags-copy-snippet");
const leaTagsCopyStatusEl = document.querySelector("#lea-tags-copy-status");

if (leaTagsCopySnippetButton) {
  leaTagsCopySnippetButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(LEA_TAGS_PREAMBLE_SNIPPET);
      if (leaTagsCopyStatusEl) leaTagsCopyStatusEl.textContent = "Snippet copied. Paste it into your project's preamble.";
    } catch (error) {
      if (leaTagsCopyStatusEl) {
        leaTagsCopyStatusEl.textContent = error instanceof Error ? error.message : String(error);
      }
    }
  });
}

function renderProviderKeyStatus(providerKeys = {}) {
  if (!providerStatusList) return;
  providerStatusList.replaceChildren();
  for (const [family, label] of Object.entries(MODEL_FAMILY_LABELS)) {
    const item = document.createElement("li");
    const configured = Boolean(providerKeys[family]?.configured);
    item.textContent = `${label}: ${configured ? "configured" : "missing"}`;
    item.dataset.configured = configured ? "true" : "false";
    providerStatusList.appendChild(item);
  }
}
