import assert from "node:assert/strict";
import test from "node:test";
import katex from "katex";
import {
  LEAN_PANE_KATEX_MACROS,
  renderPaneMath
} from "../extension/leanPaneMath.mjs";

test("vendored KaTeX supports representative theorem notation", () => {
  const html = katex.renderToString(
    String.raw`f(x) \triangleq \frac{x^2}{\sqrt{1+x}} \in \RR`,
    {
      throwOnError: true,
      trust: false,
      macros: { ...LEAN_PANE_KATEX_MACROS }
    }
  );

  assert.match(html, /≜/);
  assert.match(html, /mfrac/);
  assert.match(html, /mathvariant="double-struck">R|class="mord mathbb">R/);
  assert.doesNotMatch(html, />triangleq</);
});

test("renderPaneMath applies safe consistent KaTeX options", () => {
  const calls = [];
  const renderer = {
    render(source, element, options) {
      calls.push({ source, element, options });
    }
  };
  const element = {};
  const result = renderPaneMath(renderer, element, String.raw`a \triangleq b`, true);

  assert.equal(result.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].source, String.raw`a \triangleq b`);
  assert.equal(calls[0].element, element);
  assert.equal(calls[0].options.displayMode, true);
  assert.equal(calls[0].options.output, "htmlAndMathml");
  assert.equal(calls[0].options.throwOnError, true);
  assert.equal(calls[0].options.trust, false);
  assert.equal(calls[0].options.macros["\\RR"], "\\mathbb{R}");
});

test("renderPaneMath reports unavailable and malformed-render failures", () => {
  assert.equal(renderPaneMath(null, {}, "x").ok, false);

  const failure = new Error("bad TeX");
  const result = renderPaneMath({
    render() {
      throw failure;
    }
  }, {}, String.raw`\notACommand`);

  assert.equal(result.ok, false);
  assert.equal(result.error, failure);
});
