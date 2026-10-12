# Install the Overleaf Lea Formalizer

The VIDA Docker image includes the shared Lea adapter, prover, Lean, Mathlib, and
the Overleaf companion. Chrome loads the extension directly from the same VIDA
repository checkout.

## Requirements

- Docker Desktop
- Google Chrome
- One API key from OpenAI, Anthropic, or Google Gemini

## Start the VIDA image

```sh
git clone https://github.com/VIDA-NYU/Lea.git
cd Lea/apps/lea-standalone
docker compose pull
docker compose up
```

Keep that terminal running. Open <http://localhost:8001>, choose a model in
**Settings**, and paste the corresponding provider key.

## Load the extension

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select `apps/overleaf-extension/extension` in the VIDA checkout.
5. Open the extension options and leave the companion URL at
   `http://127.0.0.1:31245`.

Chrome remembers the unpacked extension as long as the checkout stays in the
same location.

## Mark a theorem

```tex
\begin{theorem}\label{thm:finite-tree-leaves}
% lea: formalize
Every finite tree has at least two leaves.
\end{theorem}
```

Lea creates a stable internal label automatically. The `label=...` field is
deprecated but remains supported for existing documents. If used, it must be
a valid Lean identifier: letters, digits, and underscores, with no leading digit.

## Update

Export important projects, then run:

```sh
docker compose pull
docker compose up
```

The mutable `:main` tag is published from the VIDA repository. The proof
workspace is not currently mounted on the host, so an image replacement can
discard unexported proof files.
