import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const pageBridgePath = path.join(repoRoot, "apps/overleaf-extension/extension/pageBridge.js");

test("page bridge positions target badges from badgeFrom", async () => {
  const source = [
    "\\begin{definition}\\label{def:even-nat}",
    "% lea: define label=EvenNat",
    "A natural number is even if it is twice another natural number.",
    "\\end{definition}"
  ].join("\n");
  const expectedBadgeFrom = source.indexOf("\n");
  const messages = [];
  const listeners = new Map();
  const coordPositions = [];
  const extensions = [];

  globalThis.window = {
    CodeMirror: null,
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) {
      messages.push(message);
    },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?badgeFrom=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    assert.equal(typeof installBridge, "function");

    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: {
            fromClass(PluginClass, spec) {
              return { PluginClass, spec };
            }
          },
          Decoration: {
            mark() {
              return {
                range(from, to) {
                  return { from, to };
                }
              };
            },
            set(ranges) {
              return ranges;
            }
          }
        }
      }
    });

    assert.equal(extensions.length, 1);
    const view = {
      state: {
        doc: {
          toString() {
            return source;
          }
        }
      },
      coordsAtPos(position) {
        coordPositions.push(position);
        return { left: position, right: position + 1, top: 10, bottom: 28 };
      }
    };

    new extensions[0].PluginClass(view);

    const visibleMessage = messages.find((message) => message.type === "OL_LEAN_TARGETS_VISIBLE");
    assert.ok(visibleMessage);
    assert.equal(visibleMessage.targets.length, 1);
    assert.equal(visibleMessage.targets[0].badgeFrom, expectedBadgeFrom);
    assert.equal(visibleMessage.targets[0].sourceStartLine, 1);
    assert.equal(visibleMessage.targets[0].sourceEndLine, 4);
    assert.equal(coordPositions[0], expectedBadgeFrom);
    assert.deepEqual(visibleMessage.targets[0].coords, {
      left: expectedBadgeFrom + 1,
      top: 10,
      bottom: 28
    });
  } finally {
    delete globalThis.window;
  }
});

test("page bridge publishes a tag-sourced target the same way as a comment-sourced one", async () => {
  // Regression for docs/FEATURE-overleaf-inline-lea-tags.md: pageBridge.js has
  // no syntax-specific branching (it imports parseTargetDocument directly and
  // forwards targets/diagnostics generically), so a tag-marked target in a
  // custom (non-allowlisted) environment should reach OL_LEAN_TARGETS_VISIBLE
  // with no code changes here.
  const source = [
    "\\usepackage{lea-tags}",
    "\\begin{document}",
    "\\begin{claim}\\label{clm:foo}",
    "\\leatheorem{label=foo_claim}",
    "Statement text.",
    "\\end{claim}",
    "\\end{document}"
  ].join("\n");
  const messages = [];
  const listeners = new Map();

  globalThis.window = {
    CodeMirror: null,
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) {
      messages.push(message);
    },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?tagTarget=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    assert.equal(typeof installBridge, "function");

    const extensions = [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: {
            fromClass(PluginClass, spec) {
              return { PluginClass, spec };
            }
          },
          Decoration: {
            mark() {
              return { range: (from, to) => ({ from, to }) };
            },
            set(ranges) {
              return ranges;
            }
          }
        }
      }
    });

    const view = {
      state: { doc: { toString: () => source } },
      coordsAtPos: (position) => ({ left: position, right: position + 1, top: 10, bottom: 28 })
    };
    new extensions[0].PluginClass(view);

    const visibleMessage = messages.find((message) => message.type === "OL_LEAN_TARGETS_VISIBLE");
    assert.ok(visibleMessage);
    assert.equal(visibleMessage.targets.length, 1);
    assert.equal(visibleMessage.targets[0].syntax, "tag");
    assert.equal(visibleMessage.targets[0].targetLabel, "foo_claim");
    assert.equal(visibleMessage.targets[0].latexEnvironment, "claim");
    assert.equal(visibleMessage.diagnostics.length, 0);
  } finally {
    delete globalThis.window;
  }
});

test("page bridge selects and scrolls to a source range on OL_LEAN_NAVIGATE", async () => {
  const source = [
    "\\begin{theorem}\\label{thm:main}",
    "% lea: formalize label=main_theorem",
    "A theorem.",
    "\\end{theorem}"
  ].join("\n");
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];
  let focused = false;

  globalThis.window = {
    CodeMirror: null,
    _ide: {
      editorManager: { getCurrentDocId: () => "doc-1" },
      fileTreeManager: {
        findEntityById: () => ({ _id: "doc-1", path: "main.tex" }),
        getEntityPath: (entity) => entity.path
      }
    },
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage() {},
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?navigate=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() { focused = true; }
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    assert.equal(typeof onMessage, "function");
    onMessage({
      source: globalThis.window,
      data: { type: "OL_LEAN_NAVIGATE", sourceFile: "/main.tex", from: 5, to: 20 }
    });

    assert.equal(dispatched.length, 1);
    assert.deepEqual(dispatched[0].selection, { anchor: 5, head: 20 });
    assert.equal(dispatched[0].scrollIntoView, true);
    assert.equal(focused, true);
  } finally {
    delete globalThis.window;
  }
});

test("page bridge anchors navigation on the marker label even without an active path", async () => {
  const source = [
    "\\begin{theorem}\\label{thm:main}",
    "% lea: formalize label=main_theorem",
    "A theorem.",
    "\\end{theorem}"
  ].join("\n");
  const markerIndex = source.indexOf("% lea");
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];

  globalThis.window = {
    CodeMirror: null,
    // No _ide: getActiveDocPath() returns "" — navigation must still work via anchor.
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage() {},
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?anchor=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    onMessage({
      source: globalThis.window,
      data: {
        type: "OL_LEAN_NAVIGATE",
        sourceFile: "main.tex",
        from: 0,
        to: 0,
        leanLabel: "main_theorem",
        latexLabel: "thm:main"
      }
    });

    assert.equal(dispatched.length, 1);
    assert.deepEqual(dispatched[0].selection, { anchor: markerIndex, head: markerIndex });
    assert.equal(dispatched[0].scrollIntoView, true);
  } finally {
    delete globalThis.window;
  }
});

test("page bridge does not apply cross-file offsets when active path is unknown", async () => {
  const source = [
    "\\begin{theorem}\\label{thm:main}",
    "% lea: formalize label=main_theorem",
    "A theorem.",
    "\\end{theorem}"
  ].join("\n");
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];
  const posted = [];

  globalThis.window = {
    CodeMirror: null,
    // No _ide path information: a supp.tex request must not highlight main.tex by offset.
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) { posted.push(message); },
    setTimeout() { return 0; },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?unknownPathNoOffset=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    onMessage({
      source: globalThis.window,
      data: {
        type: "OL_LEAN_NAVIGATE",
        sourceFile: "supp.tex",
        from: 0,
        to: 42,
        leanLabel: "supplemental_lemma",
        latexLabel: "lem:supp"
      }
    });

    assert.equal(dispatched.length, 0);
    const result = posted.find((message) => message.type === "OL_LEAN_NAVIGATE_RESULT");
    assert.ok(result && result.ok === false);
    assert.equal(result.reason, "open_failed");
    assert.equal(result.sourceFile, "supp.tex");
  } finally {
    delete globalThis.window;
  }
});

test("page bridge opens a different file and selects the block on cross-file navigation", async () => {
  const source = [
    "\\begin{theorem}\\label{thm:main}",
    "% lea: formalize label=main_theorem",
    "A theorem.",
    "\\end{theorem}"
  ].join("\n");
  const markerIndex = source.indexOf("% lea");
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];
  const openCalls = [];
  const posted = [];

  // Active doc starts as other.tex; opening main.tex (mock) flips the active doc.
  let currentDocId = "doc-other";
  const entitiesById = {
    "doc-other": { _id: "doc-other", path: "other.tex" },
    "doc-main": { _id: "doc-main", path: "main.tex" }
  };

  globalThis.window = {
    CodeMirror: null,
    _ide: {
      editorManager: {
        getCurrentDocId: () => currentDocId,
        openDoc(entity, options) {
          openCalls.push({ entity, options });
          currentDocId = entity._id;
        }
      },
      fileTreeManager: {
        findEntityById: (id) => entitiesById[id] || null,
        findEntityByPath: (p) => {
          const key = String(p).replace(/^\/+/, "");
          return key === "main.tex" ? entitiesById["doc-main"]
            : key === "other.tex" ? entitiesById["doc-other"]
            : null;
        },
        getEntityPath: (entity) => entity.path
      }
    },
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) { posted.push(message); },
    setTimeout() { return 0; },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?xfile=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    onMessage({
      source: globalThis.window,
      data: {
        type: "OL_LEAN_NAVIGATE",
        sourceFile: "main.tex",
        line: 2,
        from: 0,
        to: 0,
        leanLabel: "main_theorem",
        latexLabel: "thm:main"
      }
    });

    // Opened the right file natively (with gotoLine), then selected the block.
    assert.ok(openCalls.some((call) => call.entity?.path === "main.tex"));
    assert.equal(openCalls.at(-1).options.gotoLine, 2);
    assert.equal(dispatched.length, 1);
    assert.deepEqual(dispatched[0].selection, { anchor: markerIndex, head: markerIndex });
    const result = posted.find((message) => message.type === "OL_LEAN_NAVIGATE_RESULT");
    assert.ok(result && result.ok === true);
  } finally {
    delete globalThis.window;
  }
});

test("page bridge resolves nested file-tree entities and opens by doc id", async () => {
  const source = [
    "\\begin{lemma}\\label{lem:supp}",
    "% lea: formalize label=supplemental_lemma",
    "A supplemental result.",
    "\\end{lemma}"
  ].join("\n");
  const markerIndex = source.indexOf("% lea");
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];
  const openCalls = [];
  const posted = [];
  let currentDocId = "doc-main";

  const fileTree = {
    name: "",
    folders: [{
      name: "sections",
      docs: [{ _id: "doc-supp", name: "supp.tex" }]
    }],
    docs: [{ _id: "doc-main", name: "main.tex" }]
  };
  const entitiesById = {
    "doc-main": { _id: "doc-main", path: "main.tex" },
    "doc-supp": { _id: "doc-supp", path: "sections/supp.tex" }
  };

  globalThis.window = {
    CodeMirror: null,
    _ide: {
      editorManager: {
        getCurrentDocId: () => currentDocId,
        openDocId(id, options) {
          openCalls.push({ id, options });
          currentDocId = id;
        }
      },
      fileTreeManager: {
        root: fileTree,
        findEntityById: (id) => entitiesById[id] || null,
        getEntityPath: (entity) => entity.path
      }
    },
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) { posted.push(message); },
    setTimeout(callback) {
      callback();
      return 0;
    },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?nestedOpen=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    onMessage({
      source: globalThis.window,
      data: {
        type: "OL_LEAN_NAVIGATE",
        sourceFile: "sections/supp.tex",
        line: 2,
        from: 0,
        to: 0,
        leanLabel: "supplemental_lemma",
        latexLabel: "lem:supp"
      }
    });

    assert.deepEqual(openCalls, [{ id: "doc-supp", options: { gotoLine: 2 } }]);
    assert.equal(dispatched.length, 1);
    assert.deepEqual(dispatched[0].selection, { anchor: markerIndex, head: markerIndex });
    const result = posted.find((message) => message.type === "OL_LEAN_NAVIGATE_RESULT");
    assert.ok(result && result.ok === true);
  } finally {
    delete globalThis.window;
  }
});

test("page bridge reports failure when a different file cannot be opened", async () => {
  const source = "\\begin{theorem}\\label{thm:main}\n% lea: formalize label=main_theorem\nX\n\\end{theorem}";
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];
  const posted = [];

  globalThis.window = {
    CodeMirror: null,
    _ide: {
      editorManager: { getCurrentDocId: () => "doc-other", openDoc() {} },
      fileTreeManager: {
        findEntityById: () => ({ _id: "doc-other", path: "other.tex" }),
        findEntityByPath: () => null, // target file not resolvable
        getEntityPath: (entity) => entity.path
      }
    },
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) { posted.push(message); },
    setTimeout() { return 0; },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?xfilefail=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    // Different active file, target not openable, and the anchor IS present in the
    // current view (last-resort) — but it is a different file, so we must NOT select.
    onMessage({
      source: globalThis.window,
      data: { type: "OL_LEAN_NAVIGATE", sourceFile: "missing.tex", line: 2, leanLabel: "unrelated_label" }
    });

    assert.equal(dispatched.length, 0);
    const result = posted.find((message) => message.type === "OL_LEAN_NAVIGATE_RESULT");
    assert.ok(result && result.ok === false);
    assert.equal(result.sourceFile, "missing.tex");
  } finally {
    delete globalThis.window;
  }
});

test("page bridge clamps an out-of-range navigation target to the document", async () => {
  const source = "short";
  const listeners = new Map();
  const extensions = [];
  const dispatched = [];

  globalThis.window = {
    CodeMirror: null,
    _ide: {
      editorManager: { getCurrentDocId: () => "doc-1" },
      fileTreeManager: {
        findEntityById: () => ({ _id: "doc-1", path: "main.tex" }),
        getEntityPath: (entity) => entity.path
      }
    },
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage() {},
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?clamp=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: { fromClass(PluginClass, spec) { return { PluginClass, spec }; } },
          Decoration: { mark() { return { range(from, to) { return { from, to }; } }; }, set(ranges) { return ranges; } }
        }
      }
    });

    const view = {
      state: { doc: { length: source.length, toString() { return source; } } },
      coordsAtPos() { return null; },
      dispatch(transaction) { dispatched.push(transaction); },
      focus() {}
    };
    new extensions[0].PluginClass(view);

    const [onMessage] = listeners.get("message") || [];
    onMessage({
      source: globalThis.window,
      data: { type: "OL_LEAN_NAVIGATE", sourceFile: "main.tex", from: 999, to: 2000 }
    });

    assert.equal(dispatched.length, 1);
    assert.deepEqual(dispatched[0].selection, { anchor: source.length, head: source.length });
  } finally {
    delete globalThis.window;
  }
});

test("page bridge does not publish targets when CodeMirror has no coordinates", async () => {
  const source = [
    "\\begin{theorem}",
    "% lea: formalize label=offscreen_theorem",
    "An offscreen theorem.",
    "\\end{theorem}"
  ].join("\n");
  const messages = [];
  const listeners = new Map();
  const extensions = [];

  globalThis.window = {
    CodeMirror: null,
    innerHeight: 768,
    addEventListener(type, listener) {
      const current = listeners.get(type) || [];
      current.push(listener);
      listeners.set(type, current);
    },
    postMessage(message) {
      messages.push(message);
    },
    setInterval() {}
  };

  try {
    await import(`${pathToFileURL(pageBridgePath).href}?noCoords=${Date.now()}`);
    const [installBridge] = listeners.get("UNSTABLE_editor:extensions") || [];
    assert.equal(typeof installBridge, "function");

    installBridge({
      detail: {
        extensions,
        CodeMirror: {
          ViewPlugin: {
            fromClass(PluginClass, spec) {
              return { PluginClass, spec };
            }
          },
          Decoration: {
            mark() {
              return {
                range(from, to) {
                  return { from, to };
                }
              };
            },
            set(ranges) {
              return ranges;
            }
          }
        }
      }
    });

    const view = {
      state: {
        doc: {
          toString() {
            return source;
          }
        }
      },
      coordsAtPos() {
        return null;
      }
    };

    new extensions[0].PluginClass(view);

    const visibleMessage = messages.find((message) => message.type === "OL_LEAN_TARGETS_VISIBLE");
    assert.ok(visibleMessage);
    assert.deepEqual(visibleMessage.targets, []);
    assert.deepEqual(visibleMessage.diagnostics, []);
  } finally {
    delete globalThis.window;
  }
});
