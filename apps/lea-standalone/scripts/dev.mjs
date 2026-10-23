// The shared supervisor migrates once, then independently supervises API/worker.
import { fileURLToPath } from 'node:url';
const mode=process.argv.includes('--adapter-only')?'adapter':process.argv.includes('--overleaf')?'stack':'ui';
process.argv[2]=mode;
await import(fileURLToPath(new URL('../../../scripts/supervisor.mjs',import.meta.url)));
