import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";

const supervisorUrl = new URL("./process-supervisor.mjs", import.meta.url).href;
const stubborn = `
  process.on("SIGTERM", () => console.log("TERM " + process.pid));
  process.on("SIGINT", () => {});
  console.log("READY " + process.pid);
  setInterval(() => {}, 1000);
`;
const cooperative = `
  process.on("SIGTERM", () => process.exit(0));
  console.log("READY " + process.pid);
  setInterval(() => {}, 1000);
`;

async function fixture(t, code, graceMs = 5000, { viaNpm = false, handleExits = false, slowInspection = false } = {}) {
  let output = "";
  const script = `
    import { createProcessSupervisor } from ${JSON.stringify(supervisorUrl)};
    import { spawn } from "node:child_process";
    const supervisor = createProcessSupervisor({ graceMs: ${graceMs},
      onUnexpectedExit: ${handleExits ? '(label) => { console.log("HANDLED " + label); return true; }' : 'null'} });
    ${code}
  `;
  let command = process.execPath;
  let args = ["--input-type=module", "-e", script];
  let cwd;
  if (viaNpm) {
    cwd = await mkdtemp(path.join(os.tmpdir(), "lea-shutdown-npm-"));
    await writeFile(path.join(cwd, "runner.mjs"), script);
    await writeFile(path.join(cwd, "package.json"), JSON.stringify({ scripts: { dev: "node runner.mjs" } }));
    t.after(() => rm(cwd, { recursive: true, force: true }));
    command = process.platform === "win32" ? "npm.cmd" : "npm";
    args = ["run", "dev", "--silent"];
  }
  let inspectionPath;
  if (slowInspection) {
    inspectionPath = await mkdtemp(path.join(os.tmpdir(), "lea-shutdown-inspection-"));
    await writeFile(path.join(inspectionPath, "ps"), '#!/bin/sh\nsleep 0.2\nexec /bin/ps "$@"\n', { mode: 0o755 });
    t.after(() => rm(inspectionPath, { recursive: true, force: true }));
  }
  const child = spawn(command, args, {
    cwd, detached: true,
    env: { ...process.env, npm_lifecycle_event: "",
      PATH: inspectionPath ? inspectionPath + path.delimiter + process.env.PATH : process.env.PATH },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", chunk => { output += chunk; });
  child.stderr.on("data", chunk => { output += chunk; });
  const exited = once(child, "exit");
  t.after(() => {
    child.kill("SIGKILL");
    try { process.kill(-child.pid, "SIGKILL"); } catch {}
    for (const match of output.matchAll(/(?:READY|LEADER) (\d+)/g)) {
      try { process.kill(-Number(match[1]), "SIGKILL"); } catch {}
      try { process.kill(Number(match[1]), "SIGKILL"); } catch {}
    }
  });
  async function waitFor(pattern) {
    const deadline = Date.now() + 3000;
    while (!pattern.test(output)) {
      assert.equal(child.exitCode, null, output);
      assert.ok(Date.now() < deadline, `Timed out waiting for ${pattern}: ${output}`);
      await delay(20);
    }
  }
  return { child, exited, waitFor, get output() { return output; } };
}

function service(label, script) {
  return `supervisor.spawn(${JSON.stringify(label)}, process.execPath, ["-e", ${JSON.stringify(script)}], { stdio: "inherit" });`;
}

async function assertGone(pid) {
  for (let attempt = 0; attempt < 50; attempt++) {
    try { process.kill(pid, 0); } catch (error) {
      if (error.code === "ESRCH") return;
      throw error;
    }
    await delay(20);
  }
  assert.fail(`Owned process ${pid} survived shutdown`);
}

test("one Ctrl+C waits for cooperative services and releases their process groups", { timeout: 6000 }, async t => {
  const run = await fixture(t, service("adapter", cooperative));
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  const started = Date.now();
  run.child.kill("SIGINT");
  const [code] = await run.exited;
  assert.equal(code, 0, run.output);
  assert.ok(Date.now() - started < 1500, run.output);
  assert.doesNotMatch(run.output, /Forcing shutdown/);
  await assertGone(pid);
});

test("restart-managed exits keep peers running and retain bounded shutdown", { timeout: 6000 }, async t => {
  const run = await fixture(t, service("web", cooperative) + service("adapter", "process.exit(0)"),
    5000, { handleExits: true });
  await run.waitFor(/HANDLED adapter/);
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  assert.equal(run.child.exitCode, null);
  assert.doesNotMatch(run.output, /stopping the stack/);
  run.child.kill("SIGINT");
  const [code] = await run.exited;
  assert.equal(code, 0, run.output);
  await assertGone(pid);
});

test("a stuck service is killed automatically when its grace period expires", { timeout: 6000 }, async t => {
  const run = await fixture(t, service("adapter", stubborn), 250);
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  const started = Date.now();
  run.child.kill("SIGINT");
  const [code] = await run.exited;
  assert.equal(code, 0, run.output);
  assert.match(run.output, /Forcing shutdown/);
  assert.ok(Date.now() - started < 1500, run.output);
  await assertGone(pid);
});

test("a second Ctrl+C kills a stuck service immediately", { timeout: 6000 }, async t => {
  const run = await fixture(t, service("adapter", stubborn));
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  run.child.kill("SIGINT");
  await run.waitFor(/TERM /);
  const started = Date.now();
  run.child.kill("SIGINT");
  const [code] = await run.exited;
  assert.equal(code, 130, run.output);
  assert.ok(Date.now() - started < 1500, run.output);
  await assertGone(pid);
});

test("terminal Ctrl+C through npm remains graceful, then the next press forces exit", { timeout: 6000 }, async t => {
  const run = await fixture(t, service("adapter", stubborn), 5000, { viaNpm: true });
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  process.kill(-run.child.pid, "SIGINT");
  await run.waitFor(/TERM /);
  // npm versions may forward a copy or rely on process-group delivery alone.
  // Exercise the next physical press after the bounded copy window in both cases.
  await delay(150);
  assert.doesNotMatch(run.output, /Forcing shutdown/);
  const started = Date.now();
  process.kill(-run.child.pid, "SIGINT");
  await run.exited;
  assert.match(run.output, /Forcing shutdown/);
  assert.ok(Date.now() - started < 1500, run.output);
  await assertGone(pid);
});

test("npm signal copies remain one press after slow process inspection", { timeout: 6000 }, async t => {
  const run = await fixture(t, `
    process.env.npm_lifecycle_event = "dev";
    const peer = supervisor.spawn("adapter", process.execPath, ["-e", ${JSON.stringify(stubborn)}], { stdio: ["ignore", "pipe", "inherit"] });
    peer.stdout.on("data", chunk => {
      process.stdout.write(chunk);
      if (String(chunk).includes("READY")) {
        process.emit("SIGINT");
        process.emit("SIGINT");
        console.log("COPIES_DELIVERED");
      }
    });
  `, 5000, { slowInspection: true });
  await run.waitFor(/COPIES_DELIVERED/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  assert.doesNotMatch(run.output, /Forcing shutdown/);
  run.child.kill("SIGINT");
  const [code] = await run.exited;
  assert.equal(code, 130, run.output);
  await assertGone(pid);
});

for (const detached of [false, true]) {
test(`shutdown reaps ${detached ? "detached " : ""}grandchildren after their service leader exits`, { timeout: 6000 }, async t => {
  const parent = `
    const { spawn } = require("node:child_process");
    const leaf = spawn(process.execPath, ["-e", ${JSON.stringify(stubborn)}], { stdio: "inherit", detached: ${detached} });
    console.log("LEADER " + process.pid);
    process.on("SIGTERM", () => process.exit(0));
  `;
  const run = await fixture(t, service("adapter", parent), 250);
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  run.child.kill("SIGINT");
  await run.exited;
  await assertGone(pid);
});
}

test("an unexpected clean service exit also stops the remaining stack", { timeout: 6000 }, async t => {
  const run = await fixture(t, `
    const peer = supervisor.spawn("web", process.execPath, ["-e", ${JSON.stringify(stubborn)}], { stdio: ["ignore", "pipe", "inherit"] });
    peer.stdout.on("data", chunk => {
      process.stdout.write(chunk);
      if (String(chunk).includes("READY")) {
        supervisor.spawn("adapter", process.execPath, ["-e", "process.exit(0)"], { stdio: "inherit" });
      }
    });
  `, 250);
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  const [code] = await run.exited;
  assert.equal(code, 0, run.output);
  await assertGone(pid);
});

test("a spawn failure stops services already started", { timeout: 6000 }, async t => {
  const run = await fixture(t, `
    const peer = supervisor.spawn("web", process.execPath, ["-e", ${JSON.stringify(stubborn)}], { stdio: ["ignore", "pipe", "inherit"] });
    peer.stdout.on("data", chunk => {
      process.stdout.write(chunk);
      if (String(chunk).includes("READY")) supervisor.spawn("adapter", "/missing-lea-test-program", []);
    });
  `, 250);
  await run.waitFor(/READY (\d+)/);
  const pid = Number(run.output.match(/READY (\d+)/)[1]);
  const [code] = await run.exited;
  assert.equal(code, 1, run.output);
  await assertGone(pid);
});
