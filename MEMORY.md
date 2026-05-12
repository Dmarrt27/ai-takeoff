# Project Memory — AI Takeoff

Continuity record for future Claude sessions working on this project. Read this before making changes.

## Project Overview

- **Product:** Concrete quantity takeoff from construction-drawing PDFs. User uploads a bid set, Claude extracts concrete elements (footings, slabs, walls, columns, etc.), computes volumes in cubic yards, and shows them in an editable table. Corrections feed back into a lessons file that's injected into future analyses.
- **Owner:** Dmarrt27 (GitHub) — Martinez Western
- **GitHub repo:** [Dmarrt27/Claudev2](https://github.com/Dmarrt27/Claudev2), default branch `main`
- **Customer-facing URL:** https://martinezwesternaitakeoff.com (custom domain on Netlify)

## Architecture

Three deployments wired together via GitHub auto-deploy:

| Layer | Service | What it serves |
|---|---|---|
| Frontend | Netlify | `index.html` at `martinezwesternaitakeoff.com` |
| Backend | Render | Flask API at `ai-takeoff-api.onrender.com` |
| Source of truth | GitHub | `Dmarrt27/Claudev2` `main` branch |

A push to `main` triggers both Netlify and Render auto-deploys. **Don't put `render_template('index.html')` back in `app.py`'s root route** — Netlify owns the HTML; the Flask root returns a JSON status object.

## Repo Layout (current)

- `app.py` — Flask backend, all `/api/*` endpoints, Anthropic SDK calls, volume verification
- `index.html` — single-file frontend, vanilla JS, served by Netlify
- `learning.py` — extracts a "lesson" from each user correction, appends to `lessons.jsonl`, injects lessons into the Turn 1 prompt
- `concrete-takeoff/SKILL.md` — system prompt fragment loaded at boot; encodes the full extraction workflow (15 KB, ~6.8K chars after frontmatter strip → wait, now ~15K after Step 2.5/2.6 update)
- `concrete-takeoff/scripts/trapezoidal_volume.py` — geometry helpers for sloped/non-rectangular elements. Currently a **reference module only** — not imported by `app.py`. If you want the AI to call it, reference its formulas from inside `SKILL.md`; if you want Python to call it for canonical math, import and wire it into `_verify_element_volumes` (currently only handles rectangular `w × l × d × qty`).
- `lessons.jsonl` — starter seed for `/data/lessons.jsonl`; the deployed app auto-copies this on first boot
- `render.yaml` — Render blueprint; current Gunicorn timeout is **300s** (was 120s; raised because 12MB PDFs take ~2 min just to upload on free-tier bandwidth)
- `requirements.txt`, `.env.example`, `.gitignore`, `SYSTEM_PROMPT.md`

`learning copy.py` is a legacy duplicate; the active module is `learning.py`.

## Persistent storage (`/data`)

Render mounts a 1GB disk at `/data` (`disk: lessons-data` in `render.yaml`). All runtime-generated data goes there so it survives redeploys:

- `/data/lessons.jsonl` — generated lessons (auto-seeded from repo `lessons.jsonl` on first boot)
- `/data/feedback_log.jsonl` — raw append-only log of every `/api/feedback` POST
- `/data/snippets/` — base64-decoded snippet images submitted with feedback

`app.py` uses `DATA_DIR = "/data" if os.path.isdir("/data") else _HERE`. Locally without `/data`, files land in the project root (gitignored).

## Volume verification pipeline

After Claude returns its `return_takeoff` tool response, `_verify_element_volumes` in `app.py` recomputes every element's volume deterministically:

```
cubic_feet  = round(width_ft × length_ft × depth_ft × qty × WASTE_FACTOR, 2)
cubic_yards = round(cubic_feet / 27, 2)
```

Claude's reported numbers are discarded if they drift more than 1% from this canonical computation. Drift events are logged in `summary.verification.overrides` with the formula used, so corrections are auditable.

- `WASTE_FACTOR` env var (default `1.05`) — set on Render Environment tab to override globally.
- Drift threshold (`VOLUME_DRIFT_THRESHOLD`) is hardcoded at 1%; expose as env var if tuning needed.
- Only handles the rectangular formula. Trapezoidal/sloped elements should encode an **average depth** in `depth_ft` (Claude is instructed to do this in SKILL.md). If a per-element waste override or trapezoidal schema is added later, both must be threaded through `_verify_element_volumes` AND the `_TAKEOFF_TOOL` input_schema.

## Tool-call schema (Claude → Flask)

Claude is forced via `tool_choice` to call `return_takeoff` with this shape per element:

```
name, width_ft, length_ft, depth_ft, qty, cubic_feet, cubic_yards, notes
```

Plus a summary with `total_cubic_yards`, `total_cubic_feet`, `assumptions`. The schema is locked because the frontend table maps directly to these fields — changes require coordinated frontend updates.

## Learning loop

1. User makes corrections in the UI, clicks "Export Feedback"
2. Frontend simultaneously downloads `takeoff_feedback_YYYY-MM-DD.json` locally (personal record only — **not read back**) AND POSTs the same payload to `/api/feedback`
3. Server appends to `/data/feedback_log.jsonl`, then spawns a daemon thread that calls Claude to distill the corrections into a one-sentence rule
4. The rule is appended to `/data/lessons.jsonl`
5. Every future upload pulls the recent ~12 lessons via `format_lessons_for_prompt()` and prepends them to the Turn 1 prompt

Verify lessons are accumulating: GET https://ai-takeoff-api.onrender.com/api/lessons

## CORS

Currently wide-open: `CORS(app, origins='*', ...)` and `@app.after_request` adds `Access-Control-Allow-Origin: *` to every response. This is fine for a single private domain, **dangerous when the API is multi-tenant or public** because anyone can burn the user's Anthropic credit. Lock down to `https://martinezwesternaitakeoff.com` before any wider rollout.

Render's edge error pages (e.g., 502, 500 from Gunicorn timeout) are served WITHOUT CORS headers because Flask never gets a chance to run `@app.after_request`. The fix is preventing the timeout, not the CORS config.

## Known operational gotchas

- **Render free-tier cold start:** 15 min idle → service sleeps → next request takes 30-60s to wake. Browser users see hangs or "Failed to fetch." If this becomes a problem, either set up a keep-warm cron (ping `/api/health` every 14 min) or upgrade to the $7/mo plan.
- **Upload bandwidth:** Render free tier transfers files at ~100 KB/s. A 12MB PDF takes ~2 min just to upload. This eats into the request timeout budget. Long-term fix: presigned S3 upload from the browser, then send only the URL to `/api/upload`.
- **Browsers may surface "Failed to fetch" with no useful console error** when Render's edge serves a 500/502 without CORS headers. Always check Network tab in DevTools — if you see a response with no `Access-Control-Allow-Origin`, the server died during the request (timeout, OOM, crash).

## Workflow rules

1. **Always edit in `~/Projects/ai-takeoff`** (the git clone). Never in `~/Downloads/AI Development/AI Takeoff With API  28 April/` — that folder is a stale local copy with no git connection. If you find newer edits there, treat as a workflow leak: copy into the clone, commit, push, and remind the user.
2. **Push workflow:** `cd ~/Projects/ai-takeoff && git add . && git commit -m "..." && git push`. Auto-deploys fire on push.
3. **Git identity** is unconfigured globally; commits go through `-c user.email=Dmarrt27@users.noreply.github.com -c user.name=Dmarrt27`.
4. **TextEdit is dangerous for code** — smart quotes silently corrupt Python. Use VS Code or set TextEdit to plain-text + disable smart quotes.

## Multi-user readiness checklist

Track of items raised but not yet implemented:

- [x] **Persist runtime data to `/data`** — feedback, snippets, lessons all survive redeploys
- [ ] **Lock down CORS** to `https://martinezwesternaitakeoff.com` (replace `origins='*'`)
- [ ] **Add auth** — without it, anyone who finds the API URL can burn `ANTHROPIC_API_KEY` budget. Options: simple shared API key in a header, Auth0, Clerk, Supabase
- [ ] **Move PDF uploads to S3/R2** — currently uploads use Flask's `request.files` over Render's slow free-tier bandwidth. Presigned-URL pattern would offload bandwidth and disk
- [ ] **Per-element waste override** — current `WASTE_FACTOR` is global; SKILL.md allows drawings to specify otherwise, but the schema can't currently express it
- [ ] **Trapezoidal-aware verification** — `_verify_element_volumes` only does rectangular math; sloped slabs rely on Claude using average depth correctly

## Render service details

- **Service name:** `ai-takeoff-api`
- **Blueprint:** `ai-takeoff` (`exs-d7qfm0u7r5hc73e5d43g`)
- **Plan:** as of last check, ~$7.25/mo (services + 1GB disk). Adjust via `render.yaml`.
- **Required env vars:**
  - `ANTHROPIC_API_KEY` — must be set; without it `/api/upload` returns a clean error but boots fine
  - `CLAUDE_MODEL` — defaults to `claude-sonnet-4-6` (latest Sonnet generation)
  - `WASTE_FACTOR` — optional; defaults to `1.05`
  - `LESSONS_FILE` — optional override of `/data/lessons.jsonl`
- **Health endpoint:** `/api/health` returns `{ok, model, api_key_loaded, skill_loaded, skill_chars}`. Use `skill_chars` to verify SKILL.md updates landed after a deploy.

## Things Claude (the assistant) cannot do for the user

- Enter credit card / payment info — user must do this manually on Render
- Paste API keys into Render's UI — user must
- Click OAuth "Authorize" buttons — user must
- Create new third-party accounts on user's behalf
- Run `gh auth login` — needs interactive browser flow

## Conversation history

- **May 1, 2026** — Initial Render deployment setup. Blueprint linked, first deploy.
- **May 11–12, 2026** — Major reconciliation:
  - Set up local `gh` CLI + cloned repo to `~/Projects/ai-takeoff` (user had no clone before; was using GitHub web UI uploads)
  - Pushed accumulated local changes (newer `app.py`, `learning.py`, new `concrete-takeoff/SKILL.md`, `.gitignore`, etc.)
  - Fixed 500 on `/` by returning JSON instead of rendering a missing template
  - Moved feedback/lessons writes to `/data` for redeploy persistence
  - Added one-time lessons-seed migration in `learning.py`
  - Raised Gunicorn timeout from 120s → 300s (12MB PDFs were hitting timeout mid-upload)
  - Updated SKILL.md (~7.7K → ~15K chars) with Step 2.5/2.6, expanded formula table, hardened Quality Checks, worked-example section
  - Added `concrete-takeoff/scripts/trapezoidal_volume.py` (reference module, not yet wired)
  - Added Python deterministic volume verification (`_verify_element_volumes`) — replaces Claude's drifting arithmetic with `cubic_feet = w × l × d × qty × waste_factor`, audits every >1% override
