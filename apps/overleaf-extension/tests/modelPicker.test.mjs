import assert from "node:assert/strict";
import test from "node:test";

await import("../extension/modelPicker.js");

const { buildModelRows, MAX_RESULTS } = globalThis.LeaModelPicker;

const featured = [
  { value: "o4-mini", label: "o4-mini", family: "openai", tag: "Default" },
  { value: "anthropic/claude-sonnet", label: "Claude Sonnet", family: "anthropic" }
];

test("model picker shows featured models before the user searches", () => {
  const rows = buildModelRows({
    catalog: [{ value: "mistral/large", label: "mistral/large", provider: "mistral" }],
    featured,
    query: ""
  });

  assert.deepEqual(rows.map((row) => row.value), ["o4-mini", "anthropic/claude-sonnet"]);
});

test("model picker searches the exhaustive catalog by provider and caps rendered rows", () => {
  const catalog = Array.from({ length: MAX_RESULTS + 20 }, (_, index) => ({
    value: `mistral/model-${index}`,
    label: `Model ${index}`,
    provider: "mistral"
  }));

  const rows = buildModelRows({ catalog, featured, query: "mistral" });

  assert.equal(rows.length, MAX_RESULTS + 1);
  assert.equal(rows[0].custom, true);
  assert.equal(rows[1].provider, "mistral");
});

test("model picker offers arbitrary custom ids but not duplicate exact catalog ids", () => {
  const catalog = [{ value: "mistral/large", label: "mistral/large", provider: "mistral" }];

  const custom = buildModelRows({ catalog, featured, query: "acme/proof-model" });
  const exact = buildModelRows({ catalog, featured, query: "mistral/large" });

  assert.equal(custom[0].custom, true);
  assert.equal(custom[0].value, "acme/proof-model");
  assert.equal(exact.some((row) => row.custom), false);
  assert.equal(exact[0].value, "mistral/large");
});
