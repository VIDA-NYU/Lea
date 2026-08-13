/**
 * The home page. Hand-written HTML rather than markdown, because the layout is
 * the argument: hero + feature carousel → why → how it works.
 *
 * The carousel carries what used to be three separate sections (the two
 * applications, the proved/verified distinction, the extension points). Each
 * slide states one capability; the depth lives in the blog and install guide.
 */

import { highlight } from "../highlight.mjs";
import { escapeHtml } from "../templates.mjs";

const code = (source, lang) =>
  `<figure class="code"><pre class="language-${lang}"><code>${highlight(source, lang)}</code></pre>` +
  `<button class="copy" type="button" data-copy aria-label="Copy code">copy</button></figure>`;

/* ------------------------------------------------------------------ mockups */

/**
 * Each slide's visual is drawn in HTML/SVG rather than screenshotted: it stays
 * crisp at any size, follows the colour theme, and costs no image bytes. The
 * mockups illustrate real surfaces — the text in them is text the app shows.
 * They size themselves off the canvas via container query units, so the whole
 * vignette scales as one piece.
 */

/** LeaChat: transcript on the left, the Lean file it produced on the right. */
function mockProofCanvas() {
  // Lines are kept under ~47 characters so nothing is clipped at slide size.
  const lean = `import Mathlib
open Real

namespace Lea.Misc

/-- √6 is irrational: 6 is not a square. -/
lemma sqrt_six_irrational :
    Irrational (Real.sqrt 6) := by
  norm_num

/-- The sum √2 + √3 is irrational. -/
theorem irrational_sqrt_two_add_sqrt_three :
    Irrational (Real.sqrt 2 + Real.sqrt 3) := by
  rintro ⟨q, hq⟩
  have h_sq : (q : ℝ) ^ 2 = 5 + 2 * Real.sqrt 6`;

  return `<div class="slide-canvas mock mock-proof">
    <div class="mock-pane mock-chat">
      <p class="mock-who"><span class="mock-avatar">L</span>Lea</p>
      <p class="mock-heading"><span class="mock-tick">✓</span>Done — Proof complete</p>
      <p class="mock-text">
        <code>irrational_sqrt_two_add_sqrt_three</code> is proved and compiles cleanly in
        <code>Lea/Misc/SqrtIrrational.lean</code>.
      </p>
      <p class="mock-sub">How the proof works</p>
      <p class="mock-text">
        √6 is irrational by <code>norm_num</code>; squaring <code>q = √2 + √3</code> gives
        <code>q² = 5 + 2√6</code>, so √6 would be rational — contradiction.
      </p>
    </div>
    <div class="mock-pane mock-code-pane">
      <p class="mock-filetab">SqrtIrrational.lean<span class="mock-step">step 5 of 5</span></p>
      <pre><code>${highlight(lean, "lean")}</code></pre>
    </div>
    <p class="mock-status">
      <span class="mock-ok">✓ lean_check: 0 errors</span>
      <span class="mock-action">Run SafeVerify</span>
    </p>
  </div>`;
}

/** The blueprint graph, with the dashed "audit pending" treatment. */
function mockBlueprint() {
  const node = (x, y, label, meta, kind) => `
    <g class="bp-node bp-${kind}">
      <ellipse cx="${x}" cy="${y}" rx="74" ry="21"/>
      <text class="bp-name" x="${x}" y="${y - 3}" text-anchor="middle">${label}</text>
      <text class="bp-meta" x="${x}" y="${y + 10}" text-anchor="middle">${meta}</text>
    </g>`;

  return `<div class="slide-canvas mock mock-blueprint">
    <svg viewBox="0 0 340 232" role="img"
         aria-label="A blueprint graph: main_theorem depends on normal_regular_of_not_prime, which depends on sylow_transitive, which depends on stabilizer_index_prime and stabilizer_maximal. Proved-but-unaudited nodes are drawn with a dashed outline.">
      <path class="bp-edge" d="M170 45 L170 66"/>
      <path class="bp-edge" d="M170 108 L170 129"/>
      <path class="bp-edge" d="M150 170 L105 184"/>
      <path class="bp-edge" d="M190 170 L235 184"/>
      ${node(170, 24, "main_theorem", "theorem · audit pending", "pending")}
      ${node(170, 87, "normal_regular_of…", "lemma · stated", "stated")}
      ${node(170, 150, "sylow_transitive", "lemma · ready", "ready")}
      ${node(80, 205, "stabilizer_index…", "lemma · audit pending", "pending")}
      ${node(260, 205, "stabilizer_maximal", "lemma · audit pending", "pending")}
    </svg>
  </div>`;
}

/** A project's three standing documents. */
function mockProject() {
  const card = (icon, title, body) => `
    <div class="mock-card">
      <p class="mock-card-head"><span aria-hidden="true">${icon}</span>${title}</p>
      <p>${body}</p>
    </div>`;

  return `<div class="slide-canvas mock mock-project">
    <p class="mock-project-title">
      Burnside Prime Degree Theorem
      <span class="mock-ns">Lea.BurnsidePrimeDegreeTheorem</span>
    </p>
    <div class="mock-cards">
      ${card("▤", "Instructions", "Your goal and the rules for Lea. Read on every run — conventions, notation, what counts as done.")}
      ${card("◆", "Memory", "Durable facts and learnings. Both you and Lea append: what worked, what failed, dead ends to avoid.")}
      ${card("▦", "Files", "Papers and notes Lea can read while it works. PDF · TeX · Markdown · DOCX.")}
    </div>
  </div>`;
}

/** What comes wired in, versus what you would assemble yourself elsewhere. */
function mockTooling() {
  const items = [
    ["SafeVerify", "kernel-replay audit — proved vs verified"],
    ["Lean-LSP", "the language server kept warm between edits"],
    ["Loogle", "find a lemma by its type signature"],
    ["sub-agent roles", "premise-search and proof-candidate ship"],
    ["Skills / MCP", "a Lean skill and a server config, ready to extend"],
  ];

  return `<div class="slide-canvas mock mock-tooling">
    <p class="mock-tooling-head">Installed and wired on day one</p>
    <ul>
      ${items
        .map(
          ([name, gloss]) =>
            `<li><span class="mock-check" aria-hidden="true">✓</span>
          <span><b>${name}</b><span class="mock-gloss">${gloss}</span></span></li>`,
        )
        .join("\n")}
    </ul>
  </div>`;
}

function slides(ctx) {
  const overleafSnippet = `\\begin{theorem}\\label{thm:leaves}
% lea: formalize label=finite_tree_leaves
Every finite tree has at least two leaves.
\\end{theorem}`;

  const roleSnippet = `name: counterexample-hunter
description: Looks for a countermodel
tools: [read_file, lean_check, search_mathlib]
max_turns: 12`;

  return [
    {
      label: "LeaChat · proof canvas",
      title: "Prove it in the browser",
      body: "State a theorem in natural language and watch the Lean file take shape beside the transcript. Every step is a version you can walk back through, edit by hand, or hand back.",
      media: mockProofCanvas(),
    },
    {
      label: "LeaOverleaf · your LaTeX source",
      title: "Or straight from Overleaf",
      body: "Mark a theorem in your paper with a <code>% lea:</code> comment and it gets formalized in place — with <code>uses={…}</code> for dependencies and <code>context={…}</code> for strategy hints.",
      media: `<div class="slide-canvas vignette">${code(overleafSnippet, "tex")}</div>`,
      link: { href: ctx.url("/install/#overleaf-extension"), text: "Set up the extension" },
    },
    {
      label: "Blueprint · graph view",
      title: "A decomposition that cannot drift",
      body: "One <code>blueprint.md</code>, two views. A node is <em>ready</em> when its dependency closure is discharged, and its status is resolved from the latest Lean verdict — never stored as a label someone has to update.",
      media: mockBlueprint(),
    },
    {
      label: "Project · instructions & memory",
      title: "Context that outlives the run",
      body: "A project fixes a Lean namespace and carries instructions you write plus a memory file you and Lea both append to — what worked, what failed, which dead ends to avoid.",
      media: mockProject(),
    },
    {
      label: "Included · out of the box",
      title: "The Lean tooling is already wired",
      body: "A kernel-replay audit, a warm Lean language server, Loogle search over Mathlib, specialist sub-agent roles, skills and MCP all ship with Lea. On a general-purpose coding agent, every one of those is something you assemble yourself before you can do any mathematics.",
      media: mockTooling(),
      link: { href: ctx.url("/install/"), text: "What a fresh install gives you" },
    },
    {
      label: "Extensibility · a role, in YAML",
      title: "Extend it without forking it",
      body: "Skills are markdown, sub-agent roles are YAML, and tools and MCP servers share one registry. Domain knowledge belongs to the mathematician who has it.",
      media: `<div class="slide-canvas vignette">${code(roleSnippet, "yaml")}</div>`,
      link: { href: ctx.site.links.github, text: "Read the source", external: true },
    },
  ];
}

function carousel(ctx) {
  const items = slides(ctx);
  const total = items.length;

  const panels = items
    .map((slide, i) => {
      const link = slide.link
        ? `<a class="slide-link" href="${slide.link.href}"${slide.link.external ? ' target="_blank" rel="noopener"' : ""}>${slide.link.text} →</a>`
        : "";
      return `<article class="slide" role="group" aria-roledescription="slide"
               aria-label="${i + 1} of ${total}: ${escapeHtml(slide.title)}">
      <div class="shot">
        <div class="shot-bar"><span class="dots"><i></i><i></i><i></i></span>${slide.label}</div>
        ${slide.media}
      </div>
      <div class="slide-body">
        <h3>${slide.title}</h3>
        <p>${slide.body}</p>
        ${link}
      </div>
    </article>`;
    })
    .join("\n");

  const dots = items
    .map(
      (slide, i) =>
        `<button class="dot" type="button" data-dot="${i}"${i === 0 ? ' aria-current="true"' : ""}
         aria-label="${escapeHtml(slide.title)}"></button>`,
    )
    .join("");

  return `<div class="carousel" data-carousel aria-roledescription="carousel" aria-label="What Lea does">
  <div class="carousel-track" data-track tabindex="0">
${panels}
  </div>
  <div class="carousel-controls">
    <button class="carousel-arrow" type="button" data-prev aria-label="Previous slide">‹</button>
    <div class="dots-nav">${dots}</div>
    <button class="carousel-arrow" type="button" data-next aria-label="Next slide">›</button>
  </div>
</div>`;
}

/** The operational model from the paper, redrawn as a theme-aware SVG. */
function diagram() {
  const box = (x, y, w, h, title, subs, accent) => `
    <rect class="${accent ? "d-box-accent" : "d-box"}" x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/>
    <text class="d-label" x="${x + w / 2}" y="${y + 34}" text-anchor="middle">${title}</text>
    ${subs
      .map(
        (s, i) =>
          `<text class="d-sub" x="${x + w / 2}" y="${y + 58 + i * 16}" text-anchor="middle">${s}</text>`,
      )
      .join("")}`;

  return `<div class="diagram">
  <svg viewBox="0 0 880 412" role="img"
       aria-label="Two applications call one API. Inside the Lea backbone, a formalization controller, a Lean project runtime and a persistent run ledger form a cycle.">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path class="d-arrow" d="M 0 0 L 10 5 L 0 10 z"/>
      </marker>
    </defs>

    ${box(150, 8, 220, 76, "LeaChat", ["standalone web client"])}
    ${box(510, 8, 220, 76, "LeaOverleaf", ["Chrome extension + companion"])}

    <path class="d-edge" d="M 260 84 L 260 104" marker-end="url(#arrow)"/>
    <path class="d-edge" d="M 620 84 L 620 104" marker-end="url(#arrow)"/>

    <rect class="d-box-accent" x="150" y="106" width="580" height="46" rx="10"/>
    <text class="d-label" x="440" y="128" text-anchor="middle">One application-neutral API</text>
    <text class="d-sub" x="440" y="144" text-anchor="middle">runs · sessions · typed event stream</text>

    <rect class="d-frame" x="110" y="182" width="660" height="196" rx="14" stroke-dasharray="5 5"/>
    <text class="d-edge-label" x="126" y="202">the Lea backbone</text>

    <path class="d-edge" d="M 440 152 L 440 214" marker-end="url(#arrow)"/>

    ${box(130, 214, 180, 104, "Controller", ["projects · blueprints", "sub-agent roles"])}
    ${box(350, 214, 180, 104, "Lean runtime", ["lean_check · Mathlib", "LSP-warm · SafeVerify"])}
    ${box(570, 214, 180, 104, "Run ledger", ["SQLite timeline", "content-addressed"])}

    <path class="d-edge" d="M 312 266 L 344 266" marker-end="url(#arrow)"/>
    <text class="d-edge-label" x="328" y="256" text-anchor="middle">act</text>
    <path class="d-edge" d="M 532 266 L 564 266" marker-end="url(#arrow)"/>
    <text class="d-edge-label" x="548" y="256" text-anchor="middle">emit</text>

    <path class="d-edge" d="M 660 318 L 660 350 L 220 350 L 220 318" marker-end="url(#arrow)"/>
    <text class="d-edge-label" x="440" y="366" text-anchor="middle">history becomes the context for the next action</text>
  </svg>
</div>`;
}

export function homePage(ctx) {
  const { site } = ctx;

  // The directory `git clone` creates — derived so a repo rename only needs
  // `links.github` updated in site.config.mjs.
  const repoDir = site.links.github.split("/").pop();

  const dockerSnippet = `git clone ${site.links.github}.git
cd ${repoDir}/apps/lea-standalone
docker compose pull && docker compose up`;

  const localSnippet = `git clone ${site.links.github}.git
cd ${repoDir}
./install.sh --target ui --skip-verify
./start-dev.sh`;

  return `
<section class="hero">
  <div class="wrap hero-grid">
    <div class="hero-copy">
      <h1>Formalization that keeps the <em>mathematician</em> in the loop.</h1>
      <p class="hero-sub">
        Lea is an agent backbone for Lean&nbsp;4, driving two applications: a standalone web
        client and an Overleaf extension. You steer how the argument is decomposed, intervene
        while the proof is being built, and review each claim as it is established.
      </p>

      <div class="hero-actions">
        <a class="btn btn-primary" href="${ctx.url("/install/")}">Install Lea →</a>
        <a class="btn btn-ghost" href="${site.links.discord}" target="_blank" rel="noopener">Join the Discord</a>
        <a class="btn btn-ghost" href="${site.links.github}" target="_blank" rel="noopener">GitHub</a>
      </div>

      <div class="install" data-tabs>
        <div class="install-tabs" role="tablist" aria-label="Installation method">
          <button class="install-tab" role="tab" id="tab-docker" aria-controls="panel-docker" aria-selected="true">docker</button>
          <button class="install-tab" role="tab" id="tab-local" aria-controls="panel-local" aria-selected="false" tabindex="-1">local</button>
        </div>
        <div class="install-panel" role="tabpanel" id="panel-docker" aria-labelledby="tab-docker">
          ${code(dockerSnippet, "bash")}
          <p class="install-note">No toolchain to install — Lean and Mathlib are baked into the image. Then open <code>localhost:8001</code> and paste your API key into Settings.</p>
        </div>
        <div class="install-panel" role="tabpanel" id="panel-local" aria-labelledby="tab-local" hidden>
          ${code(localSnippet, "bash")}
          <p class="install-note">macOS or Linux, Node 22. The script installs <code>uv</code> and <code>elan</code> for you, and adds the Overleaf side.</p>
        </div>
      </div>
    </div>

    <div class="hero-media">
      ${carousel(ctx)}
    </div>
  </div>
</section>

<section class="section">
  <div class="wrap">
    <p class="kicker">Why another formalization system</p>
    <div class="thesis">
      <div>
        <blockquote class="thesis-quote">
          “81.7% of surveyed mathematics students and researchers preferred full or at least
          partial human control over the formalization process, and 66.7% wished to retain
          high-level strategic control even while delegating the mechanical work.”
        </blockquote>
        <p class="thesis-cite">
          Collins et al., <a href="https://arxiv.org/abs/2606.04273" target="_blank" rel="noopener">Characterizing
          initial human–AI proof formalization workflows</a> (2026)
        </p>
      </div>
      <ul class="thesis-points">
        <li>
          <strong>Autonomy-first systems put the mathematician at the endpoints.</strong>
          You supply a target and inspect a result. Recent harnesses report the consequence
          themselves: output that type-checks but needs an expert cleanup pass before anyone
          will maintain it.
        </li>
        <li>
          <strong>General coding agents are domain-neutral by construction.</strong>
          They hand you a loop and expect you to wire in the tools, prompts and scripts. You
          assemble a system before you do any mathematics.
        </li>
        <li>
          <strong>Lea is specialized, and it is a backbone.</strong>
          Lean-specific tools, project memory and blueprints come with it — and the agent is
          exposed through one API, so mathematician-facing software can be built on top rather
          than forked from it.
        </li>
      </ul>
    </div>
  </div>
</section>

<section class="section section-alt">
  <div class="wrap">
    <div class="section-head">
      <p class="kicker">How it works</p>
      <h2>One run, three moving parts</h2>
      <p>
        The prover runs in-process behind a single API. There is no separate prover service to
        start, and applications react to meaning-level facts, a file changed, a check returned,
        rather than decoding prover-specific tool output.
      </p>
    </div>

    ${diagram()}

    <div class="pipeline" style="margin-top:2rem">
      <div class="stage">
        <p class="stage-index">01</p>
        <h3>Formalization controller</h3>
        <p>
          Works on a <em>project</em> that outlives any single run: a fixed Lean namespace plus
          instructions, accumulated memory, and a blueprint decomposing the target into
          interdependent lemmas. It can delegate bounded work to sub-agents with their own
          budgets and tools.
        </p>
      </div>
      <div class="stage">
        <p class="stage-index">02</p>
        <h3>Lean project runtime</h3>
        <p>
          Executes each action inside your Lake workspace with a deliberately small tool
          surface: read, write, edit, <code>lean_check</code>, shell, Mathlib search. Your own
          tools plug into that surface with a URL and a note on when to use them. Loogle
          search, for example, takes a few fields in the UI and no code changes.
        </p>
      </div>
      <div class="stage">
        <p class="stage-index">03</p>
        <h3>Persistent run ledger</h3>
        <p>
          Every run is an ordered stream of typed events, stored as it streams. The live canvas
          and a page reload read the same bytes, and a client that drops off replays from its
          last cursor. Statuses are derived from the latest Lean verdict, never stored.
        </p>
      </div>
    </div>
  </div>
</section>
`;
}

export default homePage;
