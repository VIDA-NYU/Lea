#!/usr/bin/env node
// Launch Electron with a clean environment.
//
// Editors and extension hosts (VS Code among them) export ELECTRON_RUN_AS_NODE=1
// to their children. That turns the Electron binary into plain Node, so
// `electron .` fails with "does not provide an export named 'app'". Strip it
// before spawning. Extra arguments are passed through to Electron.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const electron = require('electron'); // from Node this resolves to the binary's path
const appDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electron, [appDir, ...process.argv.slice(2)], { stdio: 'inherit', env });
// Ctrl+C reaches Electron through the terminal's process group; keep this
// wrapper alive until the app has finished shutting its backend down.
process.on('SIGINT', () => {});
process.on('SIGTERM', () => child.kill('SIGTERM'));
child.on('exit', (code, signal) => process.exit(signal ? 1 : (code ?? 0)));
