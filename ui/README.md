# ReproAgent Runner UI

The React + Tailwind (shadcn/ui) front end of `scripts/runner_ui.py`.

`scripts/runner_ui.py` serves the built page from `ui/dist/` (`/` → `dist/index.html`, `/assets/*` → `dist/assets/`).
`dist/` is committed, so running the demo needs only Python. If `ui/dist/index.html` is missing, the server falls
back to the older inline page inside `runner_ui.py`.

## Rebuild after changing `src/`

```sh
cd ui && npm ci && npm run build
```

Then commit `ui/dist/` together with the source change.

## Develop

Start the runner on a spare port, then the Vite dev server, which proxies `/api` to it:

```sh
.venv-autose/bin/python scripts/runner_ui.py --port 8799   # from reproagent/
cd ui && npm run dev
```

- `src/lib/runner.tsx`: wording, log-line mapping and verdict logic, ported from the inline page. Keep the two in step.
- `src/index.css`: the paper-and-ink theme (light and "night paper" dark) on shadcn's CSS variables.
- `src/components/ui/`: shadcn components (`npx shadcn@latest add <name>`).
