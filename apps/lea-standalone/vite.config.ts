import { defineConfig } from 'vite'
import path from 'path'
import { createRequire } from 'node:module'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// v2.2 live editor (lean4monaco) build requirements — see
// design/v2.2-live-editor-architecture.md (D68). These mirror lean4web's client
// vite config: the monaco-vscode services need node built-ins polyfilled + an
// esbuild shim for `import.meta.url`, and the Lean InfoView webview assets must be
// copied into the served tree.
import importMetaUrlPlugin from '@codingame/esbuild-import-meta-url-plugin'
import { nodePolyfills } from 'vite-plugin-node-polyfills'
import { viteStaticCopy } from 'vite-plugin-static-copy'

const require = createRequire(import.meta.url)

// Resolve a package's on-disk dir via require.resolve so the static-copy globs
// survive hoisting (deps live in the monorepo-root node_modules today, but this
// stays correct if they de-hoist into the app's own node_modules).
function pkgDir(pkg: string): string {
  return path.dirname(require.resolve(`${pkg}/package.json`))
}
const infoviewDist = path.join(pkgDir('@leanprover/infoview'), 'dist')
const lean4monacoDist = path.join(pkgDir('lean4monaco'), 'dist')

// Must match the adapter's bind port (adapter/run_api.py reads the same variable);
// scripts/dev.mjs passes its environment through to this dev server.
const adapterPort = Number(process.env.LEA_ADAPTER_PORT) || 8001

function figmaAssetResolver() {
  return {
    name: 'figma-asset-resolver',
    resolveId(id) {
      if (id.startsWith('figma:asset/')) {
        const filename = id.replace('figma:asset/', '')
        return path.resolve(__dirname, 'src/assets', filename)
      }
    },
  }
}

export default defineConfig({
  // lean4monaco's monaco-vscode deps use `import.meta.url`; the esbuild plugin
  // rewrites it during dep pre-bundling so those workers resolve (D68).
  optimizeDeps: {
    esbuildOptions: {
      // @ts-expect-error — plugin type predates the esbuild options typings
      plugins: [importMetaUrlPlugin],
    },
  },
  plugins: [
    figmaAssetResolver(),
    // The React and Tailwind plugins are both required for Make, even if
    // Tailwind is not being actively used – do not remove them
    react(),
    tailwindcss(),
    // Polyfill node built-ins for the browser (monaco-vscode services). `fs` is
    // backed by memfs so the in-browser virtual file system works.
    nodePolyfills({
      overrides: {
        fs: 'memfs',
      },
    }),
    // Serve the Lean InfoView webview + codicon font the live editor loads at
    // runtime. Dest paths ('infoview', 'assets') match lean4monaco's expectations.
    viteStaticCopy({
      targets: [
        { src: [`${infoviewDist}/*`, `${lean4monacoDist}/webview/webview.js`], dest: 'infoview' },
        { src: [`${infoviewDist}/codicon.ttf`], dest: 'assets' },
      ],
    }),
  ],
  resolve: {
    alias: {
      // Alias @ to the src directory
      '@': path.resolve(__dirname, './src'),
    },
  },

  server: {
    // `katex` (and other deps) are hoisted to the monorepo-root node_modules, so
    // katex.min.css resolves its font files from outside the app's project root.
    // This app has its own lockfile, so Vite's workspace-root detection stops here
    // and excludes the hoisted assets; allow the monorepo root explicitly (it also
    // contains this app's dir) so those fonts serve in dev. The `..` parent is the
    // `apps/` dir and `../..` is the monorepo root.
    fs: {
      allow: [path.resolve(__dirname, '../..')],
    },
    proxy: {
      // `ws: true` upgrades the live-editor LSP WebSocket (/api/sessions/:id/lsp,
      // v2.2 · D60) to the adapter; plain /api HTTP + SSE proxy unchanged.
      '/api': { target: `http://localhost:${adapterPort}`, ws: true },
    },
    // The vendored prover holds huge Lean build trees (workspace + SafeVerify
    // .lake/ oleans, the ~189MB safe_verify binary). The frontend never imports
    // from there, so keep the file watcher out of it — otherwise the dev server
    // thrashes with full-reloads on Mathlib/SafeVerify artifacts.
    watch: {
      ignored: ['**/prover/**'],
    },
  },

  // File types to support raw imports. Never add .css, .tsx, or .ts files to this.
  assetsInclude: ['**/*.svg', '**/*.csv'],
})
