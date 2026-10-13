import net from "node:net";
import { pathToFileURL } from "node:url";

export function servicesForMode(mode) {
  if (!["adapter", "ui", "stack"].includes(mode)) {
    throw new Error("Usage: dev-stack-preflight.mjs adapter|ui|stack");
  }
  return [
    { label: "Adapter", host: "127.0.0.1", port: 8001 },
    ...(mode !== "adapter" ? [{ label: "UI", host: "0.0.0.0", port: 5173 }] : []),
    ...(mode === "stack" ? [{ label: "Overleaf companion", host: "127.0.0.1", port: 31245 }] : []),
  ];
}

export function portAvailable({ host, port }) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", (error) => {
      if (error.code === "EADDRINUSE") resolve(false);
      else reject(error);
    });
    server.listen({ host, port, exclusive: true }, () => server.close(() => resolve(true)));
  });
}

export async function assertServicesAvailable(mode, { probe = portAvailable } = {}) {
  const services = servicesForMode(mode);
  const available = await Promise.all(services.map(probe));
  const occupied = services.filter((_, index) => !available[index]);
  if (occupied.length) {
    throw new Error([
      "Cannot start another development stack; these ports are already in use:",
      ...occupied.map(({ label, port }) => `  ${label}: ${port}`),
      "Stop the existing stack with Ctrl+C in its terminal, then run the command again.",
    ].join("\n"));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await assertServicesAvailable(process.argv[2] || "stack");
  } catch (error) {
    console.error(`[start] ${error.message}`);
    process.exitCode = 1;
  }
}
