(function (global) {
  "use strict";

  const MAX_RESULTS = 60;
  let nextId = 1;

  function normalizeModel(model) {
    const value = String(model?.value || model?.id || "").trim();
    return {
      ...model,
      value,
      label: String(model?.label || value).trim(),
      provider: String(model?.provider || model?.family || "").trim(),
      tag: String(model?.tag || "").trim()
    };
  }

  function normalizeModels(models) {
    const seen = new Set();
    const normalized = [];
    for (const raw of Array.isArray(models) ? models : []) {
      const model = normalizeModel(raw);
      if (!model.value || seen.has(model.value)) continue;
      seen.add(model.value);
      normalized.push(model);
    }
    return normalized;
  }

  function buildModelRows({ catalog = [], featured = [], value = "", query = "", maxResults = MAX_RESULTS } = {}) {
    const all = new Map();
    for (const model of [...normalizeModels(catalog), ...normalizeModels(featured)]) {
      all.set(model.value, { ...all.get(model.value), ...model });
    }
    if (value && !all.has(value)) all.set(value, { value, label: value, provider: "" });
    const trimmed = String(query || "").trim();
    if (!trimmed) return [...new Set([value, ...normalizeModels(featured).map(m => m.value)])]
      .filter(v => all.has(v)).slice(0, maxResults).map(v => ({ ...all.get(v), custom: false }));
    const q = trimmed.toLowerCase();
    const matches = [...all.values()].filter(m => [m.value, m.label, m.provider].some(v => String(v || "").toLowerCase().includes(q)))
      .sort((a, b) => Number(b.value.toLowerCase() === q) - Number(a.value.toLowerCase() === q))
      .slice(0, maxResults).map(m => ({ ...m, custom: false }));
    return all.has(trimmed) ? matches : [{ value: trimmed, label: trimmed, provider: "", custom: true }, ...matches];
  }

  function catalogStatus(payload) {
    const updated = payload?.updated_at ? `Last updated ${new Date(payload.updated_at).toLocaleString()}.` : "Using bundled model data.";
    return [payload?.refreshing ? "Refreshing models…" : updated, ...(payload?.warnings || []).map(w => w.message)].filter(Boolean).join(" ");
  }

  function modelStatus(model) {
    if (model.availability === "retired") return "Retired";
    if (model.availability === "unavailable") return "Not listed for this key";
    if (model.compatibility === "unsupported") return "Unsupported for proof runs";
    if (model.compatibility === "unverified") return "Compatibility unverified";
    if (model.suggestion_reason === "new") return "Newly discovered";
    return model.preview ? "Preview" : "";
  }

  function createModelPicker({ root, value = "", catalog = [], featured = [], onChange = null, loadCatalog = null } = {}) {
    if (!root) throw new Error("A model-picker root is required.");
    if (root.leaModelPicker) {
      root.leaModelPicker.update({ value, catalog, featured, onChange, loadCatalog });
      return root.leaModelPicker;
    }

    const listId = `lea-model-picker-list-${nextId++}`;
    root.classList.add("lea-model-picker");
    root.innerHTML = `
      <button type="button" class="lea-model-picker-trigger" role="combobox" aria-label="Lea model" aria-expanded="false" aria-controls="${listId}">
        <span data-role="model-picker-value"></span><span class="lea-model-picker-caret" aria-hidden="true">⌄</span>
      </button>
      <div class="lea-model-picker-popover" hidden>
        <input class="lea-model-picker-search" type="search" autocomplete="off" spellcheck="false" aria-controls="${listId}" aria-autocomplete="list" placeholder="Search models or type any model ID…">
        <div class="lea-model-picker-heading">Suggested</div>
        <div class="lea-model-picker-status"><span class="lea-model-picker-status-text" role="status"></span><button class="lea-model-picker-refresh" type="button">Refresh models</button></div>
        <div id="${listId}" class="lea-model-picker-results" role="listbox"></div>
      </div>
    `;

    const trigger = root.querySelector(".lea-model-picker-trigger");
    const valueLabel = root.querySelector("[data-role='model-picker-value']");
    const popover = root.querySelector(".lea-model-picker-popover");
    const search = root.querySelector(".lea-model-picker-search");
    const heading = root.querySelector(".lea-model-picker-heading");
    const results = root.querySelector(".lea-model-picker-results");
    const status = root.querySelector(".lea-model-picker-status-text");
    const refreshButton = root.querySelector(".lea-model-picker-refresh");
    let loading = false, updatesDisabled = false, pollTimer;
    const state = {
      value: String(value || ""),
      catalog: normalizeModels(catalog),
      featured: normalizeModels(featured),
      onChange,
      loadCatalog,
      active: 0,
      open: false
    };

    function renderValue() {
      valueLabel.textContent = state.value || "Select or type a model";
      trigger.title = state.value || "Select model";
    }

    function rows() {
      return buildModelRows({
        value: state.value,
        catalog: state.catalog,
        featured: state.featured,
        query: search.value,
        maxResults: MAX_RESULTS
      });
    }

    function renderResults() {
      const modelRows = rows();
      heading.textContent = search.value.trim() ? "Matches" : "Suggested";
      results.replaceChildren();
      if (modelRows.length === 0) {
        const empty = document.createElement("div");
        empty.className = "lea-model-picker-empty";
        empty.textContent = "Type a model ID to use it directly.";
        results.appendChild(empty);
        return;
      }
      state.active = Math.max(0, Math.min(state.active, modelRows.length - 1));
      modelRows.forEach((model, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = `lea-model-picker-option${index === state.active ? " is-active" : ""}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", model.value === state.value ? "true" : "false");
        option.dataset.value = model.value;
        const name = document.createElement("span");
        name.className = "lea-model-picker-name";
        name.textContent = model.custom ? `Use “${model.value}”` : model.value;
        option.appendChild(name);
        if (model.provider || modelStatus(model)) {
          const meta = document.createElement("span");
          meta.className = "lea-model-picker-meta";
          meta.textContent = [model.provider, modelStatus(model)].filter(Boolean).join(" · ");
          option.appendChild(meta);
        }
        option.addEventListener("mouseenter", () => {
          if (state.active !== index) {
            state.active = index;
            renderResults();
          }
        });
        option.addEventListener("click", () => choose(model.value));
        results.appendChild(option);
      });
    }

    async function reloadCatalog(refresh = false) {
      if (loading || typeof state.loadCatalog !== "function") return;
      loading = true;
      refreshButton.disabled = true;
      try {
        const payload = await state.loadCatalog(refresh);
        if (Array.isArray(payload?.models)) state.catalog = normalizeModels(payload.models);
        if (Array.isArray(payload?.featured)) state.featured = normalizeModels(payload.featured);
        status.textContent = catalogStatus(payload);
        updatesDisabled = Boolean(payload?.updates_disabled);
        if (state.open) renderResults();
        clearTimeout(pollTimer);
        if (state.open && root.isConnected) pollTimer = setTimeout(() => { void reloadCatalog(); }, payload?.refreshing ? 2000 : 300000);
      } catch {
        status.textContent = "Could not refresh models. Showing saved data; try Refresh models.";
        clearTimeout(pollTimer);
        if (state.open && root.isConnected) pollTimer = setTimeout(() => { void reloadCatalog(); }, 30000);
      } finally {
        loading = false;
        refreshButton.disabled = updatesDisabled;
      }
    }
    refreshButton.addEventListener("click", () => { void reloadCatalog(true); });
    const onFocus = () => { if (!root.isConnected) close(); else if (state.open) void reloadCatalog(); };
    status.textContent = "Using saved model data. Search or refresh models.";

    function open() {
      state.open = true;
      global.addEventListener?.("focus", onFocus);
      state.active = 0;
      search.value = "";
      popover.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      renderResults();
      search.focus();
      void reloadCatalog();
    }

    function close() {
      state.open = false;
      global.removeEventListener?.("focus", onFocus);
      clearTimeout(pollTimer);
      popover.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    }

    function choose(nextValue) {
      const next = String(nextValue || "").trim();
      if (!next) return;
      const changed = next !== state.value;
      state.value = next;
      renderValue();
      close();
      if (changed) {
        if (typeof state.onChange === "function") state.onChange(next);
        root.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    trigger.addEventListener("click", () => state.open ? close() : open());
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        event.preventDefault();
        open();
      }
    });
    search.addEventListener("input", () => {
      state.active = 0;
      renderResults();
    });
    search.addEventListener("keydown", (event) => {
      const modelRows = rows();
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        trigger.focus();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        state.active = Math.min(state.active + 1, Math.max(0, modelRows.length - 1));
        renderResults();
        results.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        state.active = Math.max(0, state.active - 1);
        renderResults();
        results.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter" && modelRows[state.active]) {
        event.preventDefault();
        choose(modelRows[state.active].value);
      }
    });
    const onDocumentMouseDown = (event) => {
      if (state.open && !root.contains(event.target)) close();
    };
    document.addEventListener("mousedown", onDocumentMouseDown);

    const api = {
      getValue: () => state.value,
      setValue(nextValue) {
        state.value = String(nextValue || "").trim();
        renderValue();
      },
      update(next = {}) {
        if (Object.prototype.hasOwnProperty.call(next, "value")) state.value = String(next.value || "").trim();
        if (next.catalog) state.catalog = normalizeModels(next.catalog);
        if (next.featured) state.featured = normalizeModels(next.featured);
        if (Object.prototype.hasOwnProperty.call(next, "loadCatalog")) state.loadCatalog = next.loadCatalog;
        if (Object.prototype.hasOwnProperty.call(next, "onChange")) state.onChange = next.onChange;
        renderValue();
        if (state.open) renderResults();
      },
      destroy() {
        close();
        document.removeEventListener("mousedown", onDocumentMouseDown);
        global.removeEventListener?.("focus", onFocus);
        clearTimeout(pollTimer);
        delete root.leaModelPicker;
      }
    };
    root.leaModelPicker = api;
    Object.defineProperty(root, "value", {
      configurable: true,
      get: api.getValue,
      set: api.setValue
    });
    renderValue();
    return api;
  }

  global.LeaModelPicker = Object.freeze({
    MAX_RESULTS,
    catalogStatus,
    modelStatus,
    buildModelRows,
    createModelPicker,
    normalizeModels
  });
})(globalThis);
