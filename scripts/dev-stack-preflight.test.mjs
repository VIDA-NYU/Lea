import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile, copyFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { assertServicesAvailable, portAvailable, servicesForMode } from "./dev-stack-preflight.mjs";

test("preflight checks only the services requested by the launcher", () => {
  assert.deepEqual(servicesForMode("adapter").map((service) => service.port), [8001]);
  assert.deepEqual(servicesForMode("ui").map((service) => service.port), [8001, 5173]);
  assert.deepEqual(servicesForMode("stack").map((service) => service.port), [8001, 5173, 31245]);
  assert.throws(() => servicesForMode("other"), /Usage:/);
});

test("preflight reports every conflicting port in one actionable error", async () => {
  const checked = [];
  await assert.rejects(assertServicesAvailable("stack", { probe: async ({ port }) => {
    checked.push(port);
    return port === 5173;
  } }), (error) => {
    assert.match(error.message, /Adapter: 8001/);
    assert.match(error.message, /Overleaf companion: 31245/);
    assert.match(error.message, /Ctrl\+C/);
    assert.doesNotMatch(error.message, /UI: 5173/);
    return true;
  });
  assert.deepEqual(checked, [8001, 5173, 31245]);
  await assertServicesAvailable("stack", { probe: async () => true });
});

test("port probes reject an existing listener without disturbing it", async (t) => {
  const server = net.createServer();
  t.after(() => server.listening && server.close());
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = { host: "127.0.0.1", port: server.address().port };
  assert.equal(await portAvailable(address), false);
  assert.equal(server.listening, true);
  await new Promise((resolve) => server.close(resolve));
  assert.equal(await portAvailable(address), true);
});

test("a rejected --fresh launch exits before config creation or data reset", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "lea-start-preflight-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await copyFile(new URL("../start-dev.sh", import.meta.url), path.join(root, "start-dev.sh"));
  const bin = path.join(root, "bin");
  await mkdir(bin);
  await writeFile(path.join(bin, "node"), '#!/bin/sh\necho "$*" > preflight-called\nexit 17\n', { mode: 0o755 });
  await writeFile(path.join(bin, "npm"), '#!/bin/sh\ntouch reset-called\n', { mode: 0o755 });
  const child = spawn("bash", ["start-dev.sh", "--fresh"], {
    cwd: root, env: { ...process.env, PATH: `${bin}:${process.env.PATH}` }, stdio: "pipe",
  });
  const code = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", resolve);
  });
  assert.equal(code, 17);
  assert.equal((await readFile(path.join(root, "preflight-called"), "utf8")).trim(), "scripts/dev-stack-preflight.mjs stack");
  await assert.rejects(readFile(path.join(root, "reset-called")), { code: "ENOENT" });
  await assert.rejects(readFile(path.join(root, "apps/lea-standalone/config/lea.local.toml")), { code: "ENOENT" });
});
