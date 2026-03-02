# IMTX Training Dashboard — System Architecture

**Read this file before making any changes.**

## Overview

Ironman Texas training dashboard with two deployment targets:
- **API Server**: Flask app on Render (`https://garmin-sleep-api.onrender.com`)
- **Dashboard**: Static HTML on Netlify (`https://cerulean-chaja-3f6353.netlify.app/`)

## File Locations

| File | Path | Purpose |
|------|------|---------|
| API Server | `/Users/johncraig/garmin-api/app.py` | Flask API (1571 lines) |
| Dashboard | `/Users/johncraig/netlify_deploy/index.html` | Single-file SPA (1887 lines) |
| Strength UI | `/Users/johncraig/netlify_deploy/strength.html` | Strength training page |
| Deploy hook | `/Users/johncraig/garmin-api/.render-deploy-hook` | Render auto-deploy URL |
| Coaching prompt | `/Users/johncraig/garmin-api/COACHING_PRINCIPLES.md` | AI coaching system prompt |

## Data Pipelines (End-to-End)

### 1. Garmin Sleep/HRV
```
Garmin Connect → garth library (email/pw auth) → /garmin-sleep endpoint → Dashboard
```
- **Source**: Garmin Connect API via `garth` Python client
- **Auth**: `GARMIN_EMAIL`, `GARMIN_PASSWORD` env vars on Render
- **Server endpoint**: `GET /garmin-sleep`
- **Dashboard cache**: `imtx_garmin` (localStorage, 10-min TTL)
- **Fields**: body_battery, hrv, sleep_score, sleep_hours, readiness, deep_plus_rem_hours
- **No persistent storage** — fetched live each request

### 2. Garmin Activities
```
Garmin Connect → garth library → /garmin-activities endpoint → Dashboard
```
- **Server endpoint**: `GET /garmin-activities`
- **Dashboard cache**: `imtx_garmin_activities` (localStorage, 1-hr TTL)
- **No persistent storage**

### 3. Nutrition ⚠️ CRITICAL PIPELINE
```
Claude MCP tool → POST /log-nutrition (Render) → JSONBin → GET /nutrition/today → Dashboard
```
- **Write path**: MCP `log_nutrition` tool → `/log-nutrition` endpoint (requires `NUTRITION_API_KEY`)
- **Alt write path**: `POST /nutrition/log` (no auth, uses `entries[]` not `meals[]`)
- **Storage**: JSONBin bin `JSONBIN_BIN_ID` (env var on Render)
- **Read path**: `GET /nutrition/today` → reads from JSONBin
- **Dashboard cache**: `imtx_nutrition_cache` (localStorage, 30-min TTL)
- **Data format**: Keyed by date (`YYYY-MM-DD`), each date is array of meal objects + optional `_meta` entry
- **Meta entry**: `{_meta: true, bmr, exercise_calories, deficit, status}`
- **⚠️ KNOWN ISSUE (fixed 2026-03-01)**: Local MCP tool `mcp__claude_ai_Nutrition_Logger__log_nutrition` is a SEPARATE MCP server that does NOT write to the Render server's JSONBin. Meals logged via local MCP never appear on dashboard. Must use Render API endpoints to log meals.

### 4. Withings Weight/Body Comp
```
Withings OAuth2 → /withings/callback → JSONBin (token storage) → /withings/weight → Dashboard
```
- **Auth**: OAuth2 flow via `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`
- **Token storage**: JSONBin bin `JSONBIN_WITHINGS_BIN_ID`
- **Server endpoints**: `GET /withings/weight`, `GET /withings/weight-history`
- **Dashboard cache**: `imtx_withings_weight` (1-hr TTL), `imtx_weight_history` (1-hr TTL)
- **Weight filter**: Dashboard rejects readings outside 204–250 lbs (shared scale with spouse)
- **Body comp validation**: Checks body_fat_pct (5-35%), muscle_mass_lbs (120-220), bone_mass_lbs (5-12), body_water_pct (40-70%)

### 5. Strava Activities & Fitness
```
Strava OAuth2 (dashboard-side) → Strava API v3 → Dashboard calculations
```
- **Auth**: OAuth2 entirely in browser (Client ID: 205589)
- **Token storage**: localStorage (`strava_access`, `strava_refresh`, `strava_expires`)
- **API calls**: Direct from browser to `https://www.strava.com/api/v3/`
- **Dashboard cache**: `imtx_activities` (localStorage, 24-hr TTL)
- **Calculations**: CTL/ATL/TSB, weekly volume, race projections — all client-side

### 6. Strength Training
```
strength.html form → POST /strength/log → JSONBin → GET /strength/history
```
- **Storage**: JSONBin bin `JSONBIN_STRENGTH_BIN_ID`
- **Server caching**: `_strength_cache` (in-memory)

### 7. Run Benchmarks
```
POST /benchmarks/store (API key required) → JSONBin → GET /benchmarks → Dashboard
```
- **Storage**: JSONBin bin `JSONBIN_BENCHMARK_BIN_ID`

### 8. Coaching Audit
```
Brief text → POST /coaching-audit → Anthropic Claude API → AI analysis response
```
- **Model**: `claude-haiku-4-5-20251001`
- **System prompt**: `COACHING_PRINCIPLES.md`
- **Stateless** — no persistent storage

## JSONBin Bins (All on Render)

| Env Var | Purpose |
|---------|---------|
| `JSONBIN_API_KEY` | Master API key (shared across all bins) |
| `JSONBIN_BIN_ID` | Nutrition data |
| `JSONBIN_STRENGTH_BIN_ID` | Strength training sessions |
| `JSONBIN_WITHINGS_BIN_ID` | Withings OAuth tokens + weight history |
| `JSONBIN_BENCHMARK_BIN_ID` | Run benchmark activities |

## Dashboard Caching Strategy

All API calls use `resilientFetch()` which:
1. Checks localStorage cache first (if within TTL)
2. Awaits Render warmup ping (`/ping`)
3. Retries up to 3x with exponential backoff (2s, 5s, 10s)
4. On total failure, serves stale cache with age indicator
5. Stale badges show "cached Xm ago" or "cached Xh ago"

| Cache Key | TTL | Data |
|-----------|-----|------|
| `imtx_garmin` | 10 min | Sleep/HRV metrics |
| `imtx_nutrition_cache` | 30 min | Today's nutrition |
| `imtx_withings_weight` | 1 hr | Current weight/body comp |
| `imtx_weight_history` | 1 hr | Historical weights |
| `imtx_garmin_activities` | 1 hr | Recent Garmin activities |
| `imtx_activities` | 24 hr | Strava activities |
| `imtx_benchmarks` | 1 hr | Run benchmarks |

## Render Environment Variables

| Variable | Purpose |
|----------|---------|
| `GARMIN_EMAIL` | Garmin Connect login |
| `GARMIN_PASSWORD` | Garmin Connect password |
| `JSONBIN_API_KEY` | JSONBin master key |
| `JSONBIN_BIN_ID` | Nutrition bin |
| `JSONBIN_STRENGTH_BIN_ID` | Strength bin |
| `JSONBIN_WITHINGS_BIN_ID` | Withings bin |
| `JSONBIN_BENCHMARK_BIN_ID` | Benchmark bin |
| `WITHINGS_CLIENT_ID` | Withings OAuth |
| `WITHINGS_CLIENT_SECRET` | Withings OAuth |
| `WITHINGS_TOKEN` | Fallback token (JSON string) |
| `NUTRITION_API_KEY` | Auth for `/log-nutrition` |
| `ANTHROPIC_API_KEY` | Claude API for coaching |
| `VESYNC_EMAIL` | VeSync scale (inactive) |
| `VESYNC_PASSWORD` | VeSync scale (inactive) |
| `PORT` | Server port (default 5000) |

## Deployment

- **Dashboard**: `cd ~/netlify_deploy && npx netlify-cli deploy --prod --dir . --site 484ed9e6-a126-4855-a284-295957c4eb2b`
- **API**: Push to `main` branch → Render auto-deploys, or trigger deploy hook
- **Deploy hook**: `curl -X POST "$(cat ~/garmin-api/.render-deploy-hook)"`

## Known Issues & Fixes Log

| Date | Issue | Resolution |
|------|-------|------------|
| 2026-03-01 | Nutrition shows empty despite MCP logging | Local MCP tool writes to different storage than Render server. Fixed by seeding via Render `/nutrition/log` endpoint. Root cause: two separate MCP servers (local Claude Code vs Render SSE) writing to different JSONBin bins. |
