import { execFileSync, spawn, spawnSync } from "node:child_process";

// Every service owns a process group, so npm wrappers and their subprocesses
// receive the same signal. Keep the group after its leader exits: a descendant
// may still be alive and holding a socket or inherited stdout open.
export function createProcessSupervisor({ graceMs = 5000, prefix = "[dev]" } = {}) {
  const owned = [];
  const descendants = new Map();
  const controller = new AbortController();
  let stopping = false;
  let forced = false;
  let exitCode = 0;
  let deadline;
  let poll;
  let lastInterrupt = -Infinity;
  let awaitingNpmCopy = false;

  function captureDescendants() {
    if (process.platform === "win32") return new Map();
    try {
      const output = execFileSync("ps", ["-axo", "pid=,ppid=,pgid=,lstart="], {
        encoding: "utf8", timeout: 500, stdio: ["ignore", "pipe", "ignore"],
      });
      const processes = new Map(output.trim().split("\n").map(line => {
        const [pid, parent, group, ...started] = line.trim().split(/\s+/);
        return [Number(pid), { parent: Number(parent), group: Number(group), started: started.join(" ") }];
      }));
      const parents = new Set(owned.filter(({ child }) => child.exitCode === null && child.signalCode === null)
        .map(({ child }) => child.pid));
      for (const [pid, started] of descendants) {
        if (processes.get(pid)?.started === started) parents.add(pid);
      }
      let changed;
      do {
        changed = false;
        for (const [pid, info] of processes) {
          if (!parents.has(pid) && parents.has(info.parent)) {
            parents.add(pid);
            descendants.set(pid, info.started);
            changed = true;
          }
        }
      } while (changed);
      return processes;
    } catch {
      // Process-group cleanup remains available if process inspection is denied.
      return new Map();
    }
  }

  function signalDetachedDescendants(name, processes) {
    const groups = new Set(owned.map(({ child }) => child.pid));
    for (const [pid, started] of descendants) {
      const info = processes.get(pid);
      // A compiler may create its own session. Verify its start time before
      // signaling a retained PID so a reused PID can never target another job.
      if (!info || info.started !== started || groups.has(info.group)) continue;
      try { process.kill(pid, name); } catch (error) {
        if (error.code !== "ESRCH") console.error(`${prefix} Could not signal subprocess ${pid}: ${error.message}`);
      }
    }
  }

  function descendantsAlive() {
    for (const pid of descendants.keys()) {
      try { process.kill(pid, 0); return true; } catch (error) {
        if (error.code !== "ESRCH") return true;
        descendants.delete(pid);
      }
    }
    return false;
  }

  function signal(service, name) {
    if (!service.child.pid) return;
    try {
      if (process.platform === "win32") {
        if (name === "SIGKILL") {
          spawnSync("taskkill", ["/pid", String(service.child.pid), "/t", "/f"], { stdio: "ignore", timeout: 1000 });
        } else {
          service.child.kill(name);
        }
      } else {
        process.kill(-service.child.pid, name);
      }
    } catch (error) {
      if (error.code !== "ESRCH") console.error(`${prefix} Could not signal ${service.label}: ${error.message}`);
    }
  }

  function alive(service) {
    if (!service.child.pid) return false;
    if (process.platform === "win32") {
      return service.child.exitCode === null && service.child.signalCode === null;
    }
    try {
      process.kill(-service.child.pid, 0);
      return true;
    } catch (error) {
      return error.code !== "ESRCH";
    }
  }

  function finish() {
    clearTimeout(deadline);
    clearInterval(poll);
    process.exit(exitCode);
  }

  function force(code = exitCode) {
    if (forced) return;
    forced = true;
    stopping = true;
    exitCode = code;
    controller.abort();
    clearTimeout(deadline);
    clearInterval(poll);
    console.log(`${prefix} Forcing shutdown.`);
    const processes = captureDescendants();
    for (const service of owned) signal(service, "SIGKILL");
    signalDetachedDescendants("SIGKILL", processes);
    // Do not depend on exit/close events from unresponsive children or pipes.
    setTimeout(finish, 100);
  }

  function stop(code = 0) {
    if (stopping) return;
    stopping = true;
    exitCode = code;
    controller.abort();
    console.log(`${prefix} Shutting down; press Ctrl+C again to force quit.`);
    deadline = setTimeout(() => force(), graceMs);
    const processes = captureDescendants();
    // Closing the clients first releases their long-lived adapter streams.
    const services = [...owned].sort((a, b) => Number(a.label === "adapter") - Number(b.label === "adapter"));
    for (const service of services) signal(service, "SIGTERM");
    signalDetachedDescendants("SIGTERM", processes);
    poll = setInterval(() => {
      if (!owned.some(alive) && !descendantsAlive()) finish();
    }, 50);
  }

  process.on("SIGINT", () => {
    const now = performance.now();
    // npm forwards a terminal SIGINT even though this process already received
    // it from the foreground process group. Those two deliveries are one press.
    if (awaitingNpmCopy && now - lastInterrupt < 100) {
      awaitingNpmCopy = false;
      return;
    }
    awaitingNpmCopy = Boolean(process.env.npm_lifecycle_event);
    lastInterrupt = now;
    if (stopping) force(130);
    else stop();
  });
  process.on("SIGTERM", () => stopping ? force(143) : stop());

  return {
    signal: controller.signal,
    get stopping() { return stopping; },
    stop,
    spawn(label, command, args, options = {}) {
      if (stopping) throw new Error("Shutdown is already in progress.");
      const child = spawn(command, args, { ...options, detached: true });
      const service = { label, child };
      owned.push(service);
      child.on("error", (error) => {
        console.error(`${prefix} ${label} could not start: ${error.message}`);
        stop(1);
      });
      child.on("exit", (code, signalName) => {
        if (!stopping) {
          console.error(`${prefix} ${label} exited (${signalName || code}); stopping the stack.`);
          stop(code === 0 ? 0 : 1);
        }
      });
      return child;
    },
  };
}
