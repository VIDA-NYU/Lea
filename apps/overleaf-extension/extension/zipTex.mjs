// Minimal, dependency-free ZIP reader for extracting LaTeX sources from an
// Overleaf project download (`GET /project/<id>/download/zip`).
//
// Why hand-rolled: the content script must stay bundler-free, and we only need the
// text `.tex` entries — not a general archive library. We parse the ZIP central
// directory ourselves and inflate DEFLATE entries with the browser's built-in
// `DecompressionStream("deflate-raw")` (also present in Node ≥ 20, so this module is
// unit-testable). STORED (uncompressed) entries are read as-is.
//
// Loaded in the content script via `import(chrome.runtime.getURL("zipTex.mjs"))`
// (declared in web_accessible_resources) and imported directly by the Node tests —
// one source, no duplication.

const SIG_EOCD = 0x06054b50; // end of central directory
const SIG_CD = 0x02014b50;   // central directory file header
const SIG_LOCAL = 0x04034b50; // local file header

function findEocdOffset(view) {
  // The EOCD is near the end; scan backward past a possible (here unused) comment.
  const minOffset = Math.max(0, view.byteLength - 22 - 0xffff);
  for (let i = view.byteLength - 22; i >= minOffset; i -= 1) {
    if (view.getUint32(i, true) === SIG_EOCD) return i;
  }
  return -1;
}

async function inflateRaw(bytes) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("DecompressionStream is unavailable; cannot inflate the project zip.");
  }
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  const buf = await new Response(stream).arrayBuffer();
  return new Uint8Array(buf);
}

/**
 * Extract every `.tex`, `.sty`, and `.cls` entry from a ZIP archive.
 * @param {ArrayBuffer|Uint8Array} input - the raw zip bytes
 * @returns {Promise<Array<{path: string, content: string}>>} sorted by path
 */
export async function extractLatexSourcesFromZip(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const decoder = new TextDecoder("utf-8");

  const eocd = findEocdOffset(view);
  if (eocd === -1) throw new Error("Not a valid zip (no end-of-central-directory record).");
  const entryCount = view.getUint16(eocd + 10, true);
  let cursor = view.getUint32(eocd + 16, true); // central directory offset

  const out = [];
  for (let n = 0; n < entryCount; n += 1) {
    if (view.getUint32(cursor, true) !== SIG_CD) break;
    const method = view.getUint16(cursor + 10, true);
    const compressedSize = view.getUint32(cursor + 20, true);
    const nameLen = view.getUint16(cursor + 28, true);
    const extraLen = view.getUint16(cursor + 30, true);
    const commentLen = view.getUint16(cursor + 32, true);
    const localOffset = view.getUint32(cursor + 42, true);
    const name = decoder.decode(bytes.subarray(cursor + 46, cursor + 46 + nameLen));
    cursor += 46 + nameLen + extraLen + commentLen;

    if (name.endsWith("/") || !/\.(?:tex|sty|cls)$/i.test(name)) continue;

    // Local header: data begins after its own (possibly different) name/extra fields.
    if (view.getUint32(localOffset, true) !== SIG_LOCAL) continue;
    const localNameLen = view.getUint16(localOffset + 26, true);
    const localExtraLen = view.getUint16(localOffset + 28, true);
    const dataStart = localOffset + 30 + localNameLen + localExtraLen;
    const compressed = bytes.subarray(dataStart, dataStart + compressedSize);

    let content;
    if (method === 0) {
      content = decoder.decode(compressed);
    } else if (method === 8) {
      content = decoder.decode(await inflateRaw(compressed));
    } else {
      continue; // unsupported compression method (Overleaf uses store/deflate)
    }
    out.push({ path: name, content });
  }

  out.sort((a, b) => a.path.localeCompare(b.path));
  return out;
}

// Compatibility for older imports and tests.
export const extractTexFromZip = extractLatexSourcesFromZip;
