# Project Memory — AI Takeoff

This file is a continuity record for future Claude sessions working on this project. It summarizes decisions made, the current deployment state, and useful context.

## Project Overview

- **Project name:** AI Takeoff
- **GitHub repo:** `Dmarrt27/Claudev2`
- **Default branch:** `main`
- **Languages:** HTML 73.3%, Python 26.7%
- **Stack:** Python backend (likely Flask/FastAPI based on `app.py`) with HTML frontend

## Repo Contents (as of May 1, 2026)

- `MAINindex.html` — HTML frontend
- `app.py` — Python web app entry point
- `learning.py`, `learning copy.py` — Python modules
- `lessons.jsonl` — data file
- `render.yaml` — Render.com deployment config
- `requirements.txt` — Python dependencies

## Deployment Decisions

### Why NOT Netlify
Initial intent was to link GitHub to Netlify, but discovery showed this is a Python backend application, not a static site. Netlify is designed for static sites and JS/TS serverless functions — it does not host long-running Python servers. Deploying to Netlify would have only served the static HTML and broken any backend functionality.

### Why Render
The repo already had a `render.yaml` configured for Render.com, which is well-suited for Python web apps. Decision was made to continue with Render rather than refactor for Netlify.

## Current Deployment State (Render)

- **Blueprint name:** `ai-takeoff`
- **Blueprint ID:** `exs-d7qfm0u7r5hc73e5d43g`
- **Linked repo:** `Dmarrt27/Claudev2` on `main` branch
- **Service created:** `ai-takeoff-api` (web service)
- **Initial deploy commit:** `345a0dd` ("Add files via upload")
- **Auto-deploy:** Enabled — pushes to `main` automatically trigger redeploys
- **Pricing:** ~$7.25/month total ($7 services + $0.25 disks)
- **Required env var:** `ANTHROPIC_API_KEY` (set during initial deploy; updateable under `ai-takeoff-api` → Environment tab)

## Key Constraints / Things Claude Cannot Do

These came up during setup and are useful to remember for future work:

- Cannot enter credit card / payment details — user must do this manually
- Cannot enter API keys or secrets into forms — user must paste these directly
- Cannot complete OAuth authorization flows — user must click "Authorize" themselves
- Cannot create accounts on user's behalf

## Useful Follow-ups for Future Sessions

- If the first deploy failed, check Render logs under `ai-takeoff-api` for: missing start command, port binding issues, or dependency errors in `requirements.txt`
- To rotate the `ANTHROPIC_API_KEY`: Render → `ai-takeoff-api` → Environment tab
- To switch from auto-deploy to manual: Render service settings
- If cost becomes a concern, `render.yaml` can be edited to use `plan: free` and the disk can be removed (would require redeploy)

## Conversation Date

Initial setup conversation: May 1, 2026
