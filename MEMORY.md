# Project Memory — AI Takeoff

Continuity record for future Claude sessions working on this project. Read this before making changes.

## Project Overview

- **Product:** Concrete quantity takeoff from construction-drawing PDFs. User uploads a bid set, Claude extracts concrete elements (footings, slabs, walls, columns, etc.), computes volumes in cubic yards, and shows them in an editable table. Corrections feed back into a lessons file that's injected into future analyses.
- **Owner:** Dmarrt27 (GitHub) — Martinez Western
- **GitHub repo:** [Dmarrt27/ai-takeoff](https://github.com/Dmarrt27/ai-takeoff), default branch `main`. (Repo was originally `Claudev2`; renamed 2026-05-13. GitHub auto-redirects the old URL but new pushes should use the new name.)
- **Customer-facing URL:** https://martinezwesternaitakeoff.com (custom domain on Netlify)

## Architecture

Three deployments wired together via GitHub auto-deploy:

| Layer | Service | What it serves |
|---|---|---|
| Frontend | Cloudflare Workers (with Static Assets) | `index.html` at `martinezwesternaitakeoff.com` |
| Backend | Render | Flask API at `ai-takeoff-api.onrender.com` |
| Source of truth | GitHub | `Dmarrt27/ai-takeoff` `main` branch |

A push to `main` triggers both Cloudflare and Render auto-deploys. **Don't put `render_template('index.html')` back in `app.py`'s root route** — Cloudflare owns the HTML; the Flask root returns a JSON status object.

Cloudflare config lives in `wrangler.jsonc` at repo root (`"assets": { "directory": "." }` — serves files from the repo root). The Worker name is `ai-takeoff`; its preview URL is `ai-takeoff.dylanmartinez27.workers.dev`. DNS for `martinezwesternaitakeoff.com` is managed by Cloudflare (zone added 2026-05-13; nameservers `lisa.ns.cloudflare.com` + `titan.ns.cloudflare.com`). MX records and SPF TXT for Namecheap email forwarding are preserved in the Cloudflare DNS zone.

## Repo Layout (current)

- `app.py` — Flask backend, all `/api/*` endpoints, Anthropic SDK calls, volume verification. PDF text extraction uses **pypdfium2** with explicit per-page `close()` calls; was PyPDF2 until 2026-05-13 (swapped because PyPDF2 held the entire PDF tree in RAM, OOMing Render on image-heavy drawings)
- `index.html` — single-file frontend, vanilla JS, served by Netlify
- `learning.py` — extracts a "lesson" from each user correction, appends to `lessons.jsonl`, injects lessons into the Turn 1 prompt
- `concrete-takeoff/SKILL.md` — system prompt fragment loaded at boot; encodes the dimension/geometry/cross-section extraction workflow (~27K chars after frontmatter strip). Sent as a cached `cache_control` system block. Rebar, formwork, and Step 5 output-format sections were dropped 2026-05-22 — the `return_takeoff` tool captures only concrete volume, so they were dead weight on every request.
- `concrete-takeoff/scripts/trapezoidal_volume.py` — geometry helpers for sloped/non-rectangular elements. Currently a **reference module only** — not imported by `app.py`. If you want the AI to call it, reference its formulas from inside `SKILL.md`; if you want Python to call it for canonical math, import and wire it into `_verify_element_volumes` (currently only handles rectangular `w × l × d × qty`).
- `lessons.jsonl` — starter seed for `/data/lessons.jsonl`; the deployed app auto-copies this on first boot
- `render.yaml` — Render blueprint; current Gunicorn config is **`--workers 1 --worker-class gthread --threads 4 --timeout 600`**. Workers dropped 2→1 on 2026-05-13 to halve peak memory under concurrent uploads (Claude API calls are IO-bound, threads handle concurrency fine). Timeout was raised 120→300s (slow uploads of large PDFs), then 300→600s on 2026-05-21 (high-DPI tile rendering plus a larger multi-image payload lengthens each request).
- `wrangler.jsonc` — Cloudflare Workers config (added 2026-05-13). `"assets": { "directory": "." }` serves files from repo root. Cloudflare auto-deploys on push to `main`.
- `requirements.txt`, `.env.example`, `.gitignore`, `SYSTEM_PROMPT.md`

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

## Vision pipeline (drawing rendering)

`select_and_render_vision` in `app.py` decides how each PDF page reaches Claude. Construction sheets are too large to send whole — shrinking a 36" sheet to a single image (~43 DPI) left dimension text illegible, the dominant takeoff-error source. Reworked 2026-05-21:

- Pages are scored by `_score_page_priority` — schedules, foundation plans, sections, structural sheet numbers, and dense feet-inch callouts rank high.
- High-priority pages render at `TILE_RENDER_SCALE` (~151 DPI) and are sliced into overlapping ~1024px tiles, kept just under Anthropic's ~1.15 MP image cap so the API does not silently re-downsample them.
- The 200K context window is the hard limit. `VISION_TOKEN_BUDGET` (~135K estimated image tokens) buys full tiling for roughly the top 3–4 large sheets; lower-priority sheets get one reduced-resolution thumbnail; the rest are covered by extracted text only.
- Tunable via env vars, no code redeploy: `TILE_RENDER_SCALE`, `VISION_TOKEN_BUDGET`, `MAX_VISION_IMAGES`, `MAX_RENDER_PX`, `MAX_PDF_CHARS`. Lower `TILE_RENDER_SCALE` or `MAX_RENDER_PX` if Render OOMs on large renders.
- `_RENDER_LOCK` serialises page rendering so peak memory stays bounded to one bitmap under concurrent uploads.
- **Deferred:** multi-API-call batching to cover an entire large sheet set at full resolution (the single-call path caps coverage at ~3–4 fully-tiled sheets), and vector-geometry extraction.

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

When locking CORS down, allow BOTH `https://martinezwesternaitakeoff.com` and `https://www.martinezwesternaitakeoff.com` (both are attached to the Cloudflare Worker as custom domains).

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
- **Health endpoint:** `/api/health` returns `{ok, model, api_key_loaded, skill_loaded, skill_chars, vision_render_scale, vision_token_budget, pdf_engine, pdf_engine_loaded, pdf_engine_version, pdf_engine_error}`. Use `skill_chars` to verify SKILL.md updates landed after a deploy, `vision_render_scale` to confirm the high-DPI tiling build is live, and `pdf_engine_loaded` to verify pypdfium2's C binding is healthy.

## Things Claude (the assistant) cannot do for the user

- Enter credit card / payment info — user must do this manually on Render
- Paste API keys into Render's UI — user must
- Click OAuth "Authorize" buttons — user must
- Create new third-party accounts on user's behalf
- Run `gh auth login` — needs interactive browser flow

## Conversation history

- **May 22, 2026** — Token-cost cleanup (lower per-upload API spend, no accuracy change):
  - **Prompt caching.** The system prompt (`SKILL_PROMPT` body + the `return_takeoff` tool schema, which renders just before it) is now sent as a `cache_control` ephemeral block — `system_blocks` in `extract_quantities_with_claude`. The API serves it at ~10% cost on the retry call and on any upload within the 5-minute cache window. Drawing tiles are deliberately left uncached: they are unique per upload, so a breakpoint there would only pay the cache-write premium with nothing to read it back.
  - **Retry no longer re-sends drawings.** The tool-retry loop used to append to `history` (the image-bearing first turn) and re-send the whole ~135K-token tile payload up to 2×. It now seeds a separate `retry_messages` from a TEXT-ONLY copy of the first user turn (the `initial` prompt). The retry only restructures Claude's existing prose analysis into a `return_takeoff` call — it does not re-read drawings — so this is accuracy-neutral, same rationale as the single-call collapse.
  - **SKILL.md trimmed** ~29K→27K chars: removed the rebar-quantity section + weights table, the formwork-area section, and Step 5 (markdown/xlsx output). The `return_takeoff` tool captures only concrete volume, so that content produced numbers with nowhere to go and instructed work (xlsx via a skill, `/mnt/skills/...`) the API call cannot do. All dimension/geometry/cross-section workflow kept.
  - Deleted `learning copy.py` (unused legacy duplicate). Backend + skill only; `index.html` and the tool schema untouched.

- **May 21, 2026** — High-DPI drawing tiling:
  - Replaced the vision pipeline. The old path rendered each drawing page then shrank it to 1568px (~43 DPI), leaving dimension text and callouts illegible — the dominant source of takeoff error. New `select_and_render_vision` renders priority sheets at ~151 DPI and slices them into overlapping ~1024px tiles (under the API's ~1.15 MP cap so they are not re-downsampled), ranks pages by a dimension-bearing heuristic, and spends a context-window token budget on the top sheets first; lower-priority sheets get a single thumbnail, the rest are text-only. Dropped the 10-page render cap, raised text cap 40K→80K chars, raised Gunicorn timeout 300→600s.
  - Backend-only change (`app.py`, `render.yaml`); `index.html` untouched and the new response fields (`vision_tiles_rendered`, `pdf_page_count`) are additive. Verified by syntax check + unit tests of the new pure functions; not end-to-end tested before this deploy.
  - Deferred: multi-call batching for full-set coverage; vector-geometry extraction.
- **May 1, 2026** — Initial Render deployment setup. Blueprint linked, first deploy.
- **May 13, 2026** — Memory fixes + frontend host migration:
  - Render OOM'd on a large drawing render. Root cause: PyPDF2 loaded the entire PDF binary + all parsed images into RAM, then 2 Gunicorn workers doubled peak under concurrency on a 512MB Starter instance. Fix: swapped PyPDF2 → pypdfium2 with explicit per-page close(), dropped workers 2→1 with 4 threads, added `gc.collect()` after each upload, added pypdfium2 health probe at `/api/health`. User then upgraded Render instance plan separately.
  - Netlify exceeded free-tier credit limit — 21 GitHub deploys consumed 315 of 300 free credits (15 credits per deploy). Web traffic itself was ~3 credits, basically free. Diagnosis: Netlify's pricing is calibrated for low deploy frequency; the workflow of pushing per commit blew the budget.
  - Migrated frontend from Netlify → Cloudflare Workers (with Static Assets). Cloudflare auto-opened a config PR (`wrangler.jsonc`); merged. Added `martinezwesternaitakeoff.com` as a Cloudflare DNS zone (free plan), changed Namecheap nameservers to `lisa/titan.ns.cloudflare.com`, deleted Netlify-pointing A/CNAME records, attached apex + www to the Worker as Custom Domains. Email forwarding MX + SPF preserved. Deleted Netlify site. End state: zero credit anxiety on the frontend, 500 free builds/mo on Cloudflare, free DDoS + global CDN.
  - GitHub repo renamed `Dmarrt27/Claudev2` → `Dmarrt27/ai-takeoff` somewhere in this process; auto-redirects work but local clones should `git remote set-url` to the new name.
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
