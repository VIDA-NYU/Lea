import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import {
  inspectTagPreamble, installLeaTags, projectFromMetadata, createOverleafSetupClient,
  PACKAGE_LINE, STYLE_PATH
} from "../extension/leaTagsSetup.mjs";

const styleText = fs.readFileSync(new URL("../extension/assets/lea-tags.sty", import.meta.url), "utf8");
const mainText = "\\documentclass{article}\n\\begin{document}\nA theorem.\n\\end{document}\n";
const mainDocument = { id: "main-id", path: "paper/article.tex", type: "doc" };

test("preamble insertion preserves CRLF, indentation, same-line commands, and the body", () => {
  for (const source of [mainText, mainText.replace(/\n/g, "\r\n").replace("\\begin{document}", "  \\begin{document}"), "\\documentclass{article}\\begin{document}Text"]) {
    const plan = inspectTagPreamble(source);
    assert.equal(plan.kind, "insert");
    const edited = source.slice(0, plan.offset) + plan.insert + source.slice(plan.offset);
    assert.equal(inspectTagPreamble(edited).kind, "loaded");
    assert.equal(edited.replace(plan.insert, ""), source);
    if (source.includes("\r\n")) assert.ok(plan.insert.endsWith("\r\n"));
  }
});

test("recognizes existing package variants without duplicating loads", () => {
  for (const line of [
    "\\usepackage{lea-tags}", "\\usepackage [draft] { amsmath, lea-tags, amssymb }",
    "\\RequirePackage\n{lea-tags}", "\\input {lea-tags.sty}", "\\input lea-tags\n"
  ]) assert.equal(inspectTagPreamble(line + "\n" + mainText).kind, "loaded", line);
});

test("comments, escaped percent signs, verbatim, and macro bodies cannot masquerade as setup", () => {
  for (const prefix of [
    "% \\usepackage{lea-tags}\n",
    "\\verb|\\usepackage{lea-tags}|\n",
    "\\begin{comment}\\usepackage{lea-tags}\\end{comment}\n",
    "\\newcommand{\\foo}{\\begin{document}}\n"
  ]) assert.equal(inspectTagPreamble(prefix + mainText).kind, "insert", prefix);
  assert.equal(inspectTagPreamble("\\% \\usepackage{lea-tags}\n" + mainText).kind, "loaded");
  assert.equal(inspectTagPreamble("\\newcommand{\\foo}{\\usepackage{lea-tags}}\n" + mainText).kind, "manual");
});

test("existing inline definitions are preserved and ambiguous preambles are manual", () => {
  assert.equal(inspectTagPreamble("\\NewDocumentCommand{\\lea}{m g}{}\n" + mainText).kind, "inline");
  assert.equal(inspectTagPreamble("\\lstnewenvironment{leacode}[1]{}{}\n" + mainText).kind, "inline");
  for (const prefix of ["\\input{preamble}\n", "\\include{setup}\n", "\\ifdefined\\foo\\usepackage{lea-tags}\\fi\n", "\\def\\setup{\\usepackage{lea-tags}}\n",
    "\\ifdefined\\foo\\input{lea-tags}\\fi\n", "\\input{other-preamble}\\input{lea-tags}\n",
    "\\usepackage{styles/lea-tags}\n", "\\begin{comment}\n", "\\verb|unfinished\n",
    "\\AtBeginDocument{\\usepackage{lea-tags}}\n"]) {
    assert.equal(inspectTagPreamble(prefix + mainText).kind, "manual");
  }
  assert.equal(inspectTagPreamble("% \\begin{document}\n").kind, "manual");
  assert.equal(inspectTagPreamble("\\documentclass{article}\n}\n\\begin{document}").kind, "manual");
});

test("resolves the configured main document by ID through nested folders", () => {
  const model = { _id: "project", rootDoc_id: "main-id", rootFolder: [{ _id: "root", docs: [{ _id: "other", name: "main.tex" }], folders: [{ _id: "folder", name: "paper", docs: [{ _id: "main-id", name: "article.tex" }] }] }] };
  const project = projectFromMetadata(model, "project");
  assert.equal(project.main.path, "paper/article.tex");
  assert.throws(() => projectFromMetadata(model, "other"), /mismatched/);
});

function fixture(options = {}) {
  const hasStyle = Object.hasOwn(options, "existing");
  const state = {
    project: { projectId: "project", rootId: "root", main: options.noMain ? null : mainDocument, entities: hasStyle ? [{ id: "style-id", path: STYLE_PATH, type: options.type || "doc" }] : [] },
    files: [{ path: mainDocument.path, content: options.main || mainText }, ...(hasStyle ? [{ path: STYLE_PATH, content: options.existing }] : [])],
    actions: [], progress: [], waits: [], archives: 0, created: 0
  };
  const client = {
    check() { if (options.cancelled) throw Object.assign(new Error("Cancelled"), { code: "cancelled" }); },
    async inspect() { return state.project; },
    async archive() {
      state.archives++;
      if (options.archiveError) throw new Error("Archive failed");
      return state.files.map(file => ({ ...file, content: options.stale && file.path === STYLE_PATH ? "stale" : file.content }));
    },
    async createStyle() {
      state.created++;
      if (options.createError) throw new Error("Creation failed");
      const doc = { id: "new-style-id", path: STYLE_PATH, type: "doc" };
      state.project.entities.push(doc);
      state.files.push({ path: STYLE_PATH, content: "" });
      return doc;
    }
  };
  const bridge = async (action, payload) => {
    state.actions.push(action);
    if (action === "read") return { source: state.files.find(file => file.path === payload.document.path).content };
    if (action === "peek") return options.liveStyle ? { document: { id: "style-id", path: STYLE_PATH }, source: options.liveStyle } : {};
    if (action === "populate") {
      if (options.populateError) throw new Error("Style changed");
      state.files.find(file => file.path === STYLE_PATH).content = payload.styleText;
    }
    if (action === "enable") {
      if (options.enableError) throw new Error("Enable failed");
      const file = state.files.find(file => file.path === payload.document.path);
      if (options.concurrentLoad) file.content = PACKAGE_LINE + "\n" + file.content;
      const plan = inspectTagPreamble(file.content);
      if (plan.kind === "insert") file.content = file.content.slice(0, plan.offset) + plan.insert + file.content.slice(plan.offset);
      return { kind: plan.kind === "insert" ? "loaded" : plan.kind, changed: plan.kind === "insert" };
    }
  };
  const run = () => installLeaTags({ client, bridge, styleText, onProgress: text => state.progress.push(text), wait: async ms => state.waits.push(ms) });
  return { state, run };
}

test("fresh setup creates one exact style and one package load; a second click is a no-op", async () => {
  const { state, run } = fixture();
  const result = await run();
  assert.equal(result.manual, false);
  assert.equal(state.created, 1);
  assert.equal(state.files.find(file => file.path === STYLE_PATH).content, styleText);
  assert.equal(state.files[0].content.match(/\\usepackage\{lea-tags\}/g).length, 1);
  assert.deepEqual(state.progress, ["Checking project", "Creating style file", "Verifying", "Enabling tags", "Verifying"]);
  const repeated = await run();
  assert.match(repeated.message, /Already enabled/);
  assert.equal(repeated.changed, false);
  assert.equal(state.created, 1);
});

test("matching styles tolerate CRLF and are reused", async () => {
  const { state, run } = fixture({ existing: styleText.replace(/\n/g, "\r\n") });
  assert.equal((await run()).style, "existing");
  assert.equal(state.created, 0);
  assert.ok(!state.actions.includes("populate"));
});

test("customized, empty, binary, and folder collisions are preserved", async () => {
  for (const options of [{ existing: "custom" }, { existing: "" }, { existing: styleText, type: "file" }, { existing: styleText, type: "folder" }]) {
    const { state, run } = fixture(options);
    const result = await run();
    assert.equal(result.manual, true);
    assert.equal(state.created, 0);
    assert.ok(!state.actions.includes("enable"));
    assert.equal(state.files[1].content, options.existing);
  }
});

test("a customized style already loaded stays in use without modification", async () => {
  const { state, run } = fixture({ existing: "custom", main: PACKAGE_LINE + "\n" + mainText });
  const result = await run();
  assert.equal(result.manual, false);
  assert.match(result.message, /contents were left unchanged/);
  assert.ok(!state.actions.includes("populate") && !state.actions.includes("enable"));
});

test("inline definitions require no file creation; unknown main documents retain the style", async () => {
  const inline = fixture({ main: "\\NewDocumentCommand{\\lea}{m}{}\n" + mainText });
  assert.equal((await inline.run()).style, "not-needed");
  assert.equal(inline.state.archives, 0);
  assert.equal(inline.state.created, 0);
  const missing = fixture({ noMain: true });
  assert.equal((await missing.run()).manual, true);
  assert.equal(missing.state.created, 1);
  assert.ok(!missing.state.actions.includes("enable"));
});

test("live customizations and a collaborator's new package load are respected", async () => {
  const live = fixture({ existing: styleText, liveStyle: "unsaved customization" });
  assert.equal((await live.run()).style, "custom");
  const concurrent = fixture({ concurrentLoad: true });
  await concurrent.run();
  assert.equal(concurrent.state.files[0].content.match(/\\usepackage\{lea-tags\}/g).length, 1);
});

test("partial errors identify created files and never proceed or delete them", async () => {
  for (const options of [{ populateError: true }, { enableError: true }]) {
    const { state, run } = fixture(options);
    await assert.rejects(run, error => {
      assert.match(error.message, /lea-tags.sty was created/);
      assert.equal(error.setupResult.changed, true);
      return true;
    });
    assert.equal(state.created, 1);
    assert.ok(!state.actions.includes("delete"));
    if (options.populateError) assert.ok(!state.actions.includes("enable"));
  }
});

test("verification retries once after two seconds and never claims success on stale archives", async () => {
  const { state, run } = fixture({ stale: true });
  await assert.rejects(run, /Saving could not be confirmed/);
  assert.deepEqual(state.waits, [2000]);
  assert.ok(!state.actions.includes("enable"));
});

test("creation, archive, and cancellation failures do not enable the package", async () => {
  for (const options of [{ createError: true }, { archiveError: true }, { cancelled: true }]) {
    const { state, run } = fixture(options);
    await assert.rejects(run);
    assert.ok(!state.actions.includes("enable"));
  }
});

test("transport uses root creation, the session CSRF header, and no replacement upload", async () => {
  const calls = [];
  const client = createOverleafSetupClient({
    projectId: "project", isCurrent: () => true,
    documentImpl: { querySelector: () => ({ content: "test-csrf" }) },
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return new Response(JSON.stringify({ _id: "new-id", name: STYLE_PATH }));
    }
  });
  assert.equal((await client.createStyle(null)).id, "new-id");
  assert.equal(calls[0].path, "/project/project/doc");
  assert.deepEqual(JSON.parse(calls[0].options.body), { name: STYLE_PATH, parent_folder_id: null });
  assert.equal(calls[0].options.headers["X-Csrf-Token"], "test-csrf");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.redirect, "error");
});

test("missing CSRF never submits a write and permission failures are actionable", async () => {
  let calls = 0;
  const client = createOverleafSetupClient({
    projectId: "project", isCurrent: () => true,
    documentImpl: { querySelector: () => null },
    fetchImpl: async () => { calls++; return new Response("", { status: 403 }); }
  });
  await assert.rejects(() => client.createStyle(null), /token is missing/);
  assert.equal(calls, 0);
  await assert.rejects(() => client.archive(), /permission to edit/);
});

test("lost creation response re-inspects but does not assume an existing file is owned", async () => {
  let writes = 0;
  const model = { _id: "project", rootFolder: [{ _id: "root", docs: [{ _id: "existing", name: STYLE_PATH }] }] };
  const client = createOverleafSetupClient({
    projectId: "project", isCurrent: () => true,
    documentImpl: { querySelector: () => ({ content: "test-csrf" }) },
    parseHtml: () => ({ querySelector: selector => selector.includes("ol-project_id") ? { content: "project" } : { content: JSON.stringify(model) } }),
    fetchImpl: async (_path, options) => {
      if (options.method === "POST") { writes++; throw new Error("Lost reply"); }
      return new Response("editor");
    }
  });
  await assert.rejects(() => client.createStyle("root"), /creation could not be confirmed/);
  assert.equal(writes, 1);
});

test("current Overleaf's wrapped inventory uses the main document from its settings interface", async () => {
  const client = createOverleafSetupClient({
    projectId: "project", isCurrent: () => true, documentImpl: {},
    parseHtml: () => ({ querySelector: selector => selector.includes("ol-project_id") ? { content: "project" } : null }),
    fetchImpl: async path => new Response(path.endsWith("/entities") ? JSON.stringify({ project_id: "project", entities: [{ path: "/paper/article.tex", type: "doc" }] }) : "editor"),
    bridge: async action => { assert.equal(action, "context"); return { main: mainDocument }; }
  });
  const project = await client.inspect();
  assert.deepEqual(project.main, mainDocument);
  assert.equal(project.entities[0].path, mainDocument.path);
  assert.equal(project.rootId, null);
});
