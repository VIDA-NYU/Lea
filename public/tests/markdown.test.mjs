/**
 * Pins the Markdown subset the site is written in.
 *   node --test public/tests/
 */

import test from "node:test";
import assert from "node:assert/strict";

import { render, parseFrontmatter, excerpt, slugify } from "../lib/markdown.mjs";
import { highlight } from "../lib/highlight.mjs";

const html = (source, options) => render(source, options).html;

test("escapes HTML in text", () => {
  assert.match(html("A <script>alert(1)</script> tag"), /&lt;script&gt;/);
  assert.doesNotMatch(html("A <script>alert(1)</script> tag"), /<script>/);
});

test("headings get slug ids, and repeats are disambiguated", () => {
  const { html: out, headings } = render("## Set up\n\ntext\n\n## Set up\n");
  assert.match(out, /<h2 id="set-up">/);
  assert.match(out, /<h2 id="set-up-1">/);
  assert.deepEqual(
    headings.map((h) => h.id),
    ["set-up", "set-up-1"],
  );
});

test("a single newline inside a paragraph is a space, not a break", () => {
  const out = html("one line\nwrapped for readability");
  assert.equal(out, "<p>one line wrapped for readability</p>");
});

test("two trailing spaces force a hard break", () => {
  assert.match(html("first  \nsecond"), /first<br>/);
});

test("inline markup", () => {
  assert.match(html("**bold**"), /<strong>bold<\/strong>/);
  assert.match(html("_italic_"), /<em>italic<\/em>/);
  assert.match(html("`a * b`"), /<code>a \* b<\/code>/);
  assert.match(html("~~gone~~"), /<del>gone<\/del>/);
});

test("emphasis does not fire inside snake_case identifiers", () => {
  assert.doesNotMatch(html("use lean_check_now here"), /<em>/);
});

test("code spans are immune to the link and emphasis passes", () => {
  const out = html("`[not a link](x)` and `**not bold**`");
  assert.doesNotMatch(out, /<a /);
  assert.doesNotMatch(out, /<strong>/);
});

test("links: external gets target, internal is rewritten onto the base path", () => {
  const external = html("[docs](https://example.com)");
  assert.match(external, /target="_blank" rel="noopener"/);

  const internal = html("[install](/install/)", { base: "/site/" });
  assert.match(internal, /href="\/site\/install\/"/);
  assert.doesNotMatch(internal, /target=/);
});

test("images render with lazy loading", () => {
  assert.match(html("![alt text](/a.png)"), /<img src="\/a\.png" alt="alt text".*loading="lazy"/);
});

test("fenced code is highlighted and gets a copy button", () => {
  const out = html("```bash\ngit clone x\n```");
  assert.match(out, /<pre class="language-bash">/);
  assert.match(out, /tok-keyword">git</);
  assert.match(out, /data-copy/);
});

test("fenced code contents are never treated as markdown", () => {
  const out = html("```\n# not a heading\n- not a list\n```");
  assert.doesNotMatch(out, /<h1/);
  assert.doesNotMatch(out, /<li>/);
});

test("unordered and ordered lists, with the author's own start number", () => {
  assert.match(html("- one\n- two"), /<ul><li>one<\/li>/);
  assert.match(html("1. one\n2. two"), /<ol><li>one<\/li>/);
  assert.match(html("3. three"), /<ol start="3">/);
});

test("nested list items", () => {
  const out = html("- outer\n  - inner");
  assert.match(out, /<li>outer\n<ul><li>inner<\/li><\/ul><\/li>/);
});

test("tables render with alignment and a scroll wrapper", () => {
  const out = html("| a | b |\n| --- | ---: |\n| 1 | 2 |");
  assert.match(out, /<div class="table-scroll">/);
  assert.match(out, /<th>a<\/th>/);
  assert.match(out, /<td style="text-align:right">2<\/td>/);
});

test("blockquotes, and Note: callouts", () => {
  assert.match(html("> plain quote"), /<blockquote><p>plain quote<\/p><\/blockquote>/);
  assert.match(html("> **Note.** watch out"), /class="callout callout-note"/);
  assert.match(html("> **Warning.** danger"), /callout-warning/);
});

test("thematic breaks", () => {
  assert.equal(html("---"), "<hr>");
});

test("raw HTML blocks pass through", () => {
  assert.match(html('<div class="x">hi</div>'), /<div class="x">hi<\/div>/);
});

test("frontmatter parses strings and lists, and is stripped from the body", () => {
  const { data, body } = parseFrontmatter(
    '---\ntitle: "A post"\ndate: 2026-08-11\ntags: [one, two]\n---\nBody text.\n',
  );
  assert.equal(data.title, "A post");
  assert.equal(data.date, "2026-08-11");
  assert.deepEqual(data.tags, ["one", "two"]);
  assert.equal(body.trim(), "Body text.");
});

test("a document with no frontmatter is returned whole", () => {
  const { data, body } = parseFrontmatter("Just text.\n");
  assert.deepEqual(data, {});
  assert.equal(body, "Just text.\n");
});

test("excerpt skips headings and code, and truncates on a word boundary", () => {
  const text = excerpt("# Title\n\n```\ncode\n```\n\n" + "word ".repeat(80), 40);
  assert.ok(!text.includes("Title"));
  assert.ok(!text.includes("code"));
  assert.ok(text.length <= 41, `got length ${text.length}`);
  assert.match(text, /…$/);
});

test("slugify", () => {
  assert.equal(slugify("Option A — Docker, no toolchain"), "option-a-docker-no-toolchain");
});

test("highlighter escapes, and never matches mid-identifier", () => {
  assert.match(highlight("<x>", "bash"), /&lt;/);
  assert.doesNotMatch(highlight("<x>", "bash"), /<x>/);
  assert.doesNotMatch(highlight("ungit foo", "bash"), /tok-keyword/);
  assert.match(highlight("theorem foo", "lean"), /tok-keyword">theorem</);
  assert.match(highlight("sorry", "lean"), /tok-danger">sorry</);
});

test("an unknown language is escaped but left unhighlighted", () => {
  assert.equal(highlight("a < b", "brainfuck"), "a &lt; b");
});
