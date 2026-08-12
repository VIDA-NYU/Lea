(function (global) {
  "use strict";

  const MAX_RESULTS = 60;
  let nextId = 1;

  function normalizeModel(model) {
    const value = String(model?.value || model?.id || "").trim();
    return {
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

  function buildModelRows({ catalog = [], featured = [], query = "", maxResults = MAX_RESULTS } = {}) {
    const normalizedCatalog = normalizeModels(catalog);
    const normalizedFeatured = normalizeModels(featured);
    const trimmed = String(query || "").trim();
    if (!trimmed) {
      return normalizedFeatured.slice(0, maxResults).map((model) => ({ ...model, custom: false }));
    }
    const q = trimmed.toLowerCase();
    const known = [...normalizedCatalog, ...normalizedFeatured].some((model) => model.value === trimmed);
    const matches = normalizedCatalog
      .filter((model) =>
        model.value.toLowerCase().includes(q)
        || model.label.toLowerCase().includes(q)
        || model.provider.toLowerCase().includes(q)
      )
      .slice(0, maxResults)
      .map((model) => ({ ...model, custom: false }));
    return known
      ? matches
      : [{ value: trimmed, label: trimmed, provider: "", tag: "", custom: true }, ...matches];
  }

  function createModelPicker({ root, value = "", catalog = [], featured = [], onChange = null } = {}) {
    if (!root) throw new Error("A model-picker root is required.");
    if (root.leaModelPicker) {
      root.leaModelPicker.update({ value, catalog, featured, onChange });
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
        <div class="lea-model-picker-heading">Featured</div>
        <div id="${listId}" class="lea-model-picker-results" role="listbox"></div>
      </div>
    `;

    const trigger = root.querySelector(".lea-model-picker-trigger");
    const valueLabel = root.querySelector("[data-role='model-picker-value']");
    const popover = root.querySelector(".lea-model-picker-popover");
    const search = root.querySelector(".lea-model-picker-search");
    const heading = root.querySelector(".lea-model-picker-heading");
    const results = root.querySelector(".lea-model-picker-results");
    const state = {
      value: String(value || ""),
      catalog: normalizeModels(catalog),
      featured: normalizeModels(featured),
      onChange,
      active: 0,
      open: false
    };

    function renderValue() {
      valueLabel.textContent = state.value || "Select or type a model";
      trigger.title = state.value || "Select model";
    }

    function rows() {
      return buildModelRows({
        catalog: state.catalog,
        featured: state.featured,
        query: search.value,
        maxResults: MAX_RESULTS
      });
    }

    function renderResults() {
      const modelRows = rows();
      heading.textContent = search.value.trim() ? "Matches" : "Featured";
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
        if (model.provider || model.tag) {
          const meta = document.createElement("span");
          meta.className = "lea-model-picker-meta";
          meta.textContent = model.provider || model.tag;
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

    function open() {
      state.open = true;
      state.active = 0;
      search.value = "";
      popover.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      renderResults();
      search.focus();
    }

    function close() {
      state.open = false;
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
        if (Object.prototype.hasOwnProperty.call(next, "onChange")) state.onChange = next.onChange;
        renderValue();
        if (state.open) renderResults();
      },
      destroy() {
        document.removeEventListener("mousedown", onDocumentMouseDown);
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
    buildModelRows,
    createModelPicker,
    normalizeModels
  });
})(globalThis);
