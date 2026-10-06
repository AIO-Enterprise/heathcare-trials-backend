# Health Agent

Weekly probe-and-report agent. Runs five layers of health checks against the
running stack, writes a Markdown report to `reports/health-YYYY-MM-DD.md`, and
commits it to git.

## Layers

| Layer       | What it probes                                                   |
|-------------|------------------------------------------------------------------|
| `infra`     | Docker stack, backend `/api/health`, frontend, DB enum integrity |
| `providers` | Bedrock, OpenAI, ElevenLabs, Google Ads OAuth, Meta Graph API    |
| `routes`    | Logs in as seed admin, hits one cheap GET per router             |
| `services`  | Imports every service module + makes one real Bedrock call       |
| `e2e`       | Runs `tests/seed.py` end-to-end (onboarding → upload → train)    |

## Run it manually

```powershell
# Whole sweep (all 5 layers, commits report)
python -m scripts.health_agent.run_weekly

# Subset
python -m scripts.health_agent.run_weekly --layers infra,routes

# Don't commit (useful while developing)
python -m scripts.health_agent.run_weekly --no-commit
```

Exit code is `0` if all probes pass/skip, `1` if anything fails.

## Prerequisites

1. Backend + frontend running on the ports in `docker-compose.yml`
   (backend 8001, frontend 5173). Override with env vars
   `HEALTH_BACKEND_URL`, `HEALTH_FRONTEND_URL` if your setup differs.
2. Seed admin user exists. Run once:
   ```powershell
   python tests/seed.py
   ```
   Default creds in `config.py`: `admin@medicare.com` / `medicare123`.
   Override via `HEALTH_ADMIN_EMAIL`, `HEALTH_ADMIN_PASSWORD`.
3. Provider keys in `.env` (probes auto-skip when keys are missing):
   `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_CLIENT_ID`,
   `GOOGLE_CLIENT_SECRET`, `META_APP_ID`, plus `USE_BEDROCK=true` +
   `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for Bedrock.

## Schedule it weekly (Windows Task Scheduler)

1. Open **Task Scheduler** → **Create Basic Task**.
2. Name: `Health Care Agentic — Weekly Health Check`.
3. Trigger: **Weekly** → pick a day/time (e.g. Monday 09:00).
4. Action: **Start a program**
   - Program/script: `d:\AIO\Marketing AI\Health_Care_Agentic\scripts\health_agent\run_weekly.bat`
   - Start in: `d:\AIO\Marketing AI\Health_Care_Agentic`
5. Finish. To smoke-test, right-click the task → **Run**.

stdout/stderr from each run is written to
`scripts\health_agent\last_run.log` (overwritten every run). The committed
Markdown report is the durable record.

## What the report looks like

```
# Health check 2026-05-26 — PASS

- Started: 2026-05-26T09:00:11
- Duration: 47.3s
- Summary: 28 pass / 0 fail / 4 skip

## infra
| Module           | Status | Time   | Note                  |
|------------------|--------|--------|-----------------------|
| docker_stack     | PASS   | 412ms  | 2/2 services running  |
| backend_health   | PASS   | 38ms   | version 1.0.0         |
| ...
```

On any failure, the headline flips to `FAIL` and a `## Failures` section
appends each error with traceback.

## Tuning / cost notes

- `services.bedrock_client_call` makes one real Bedrock call per run.
- `e2e.onboarding_pipeline` runs `seed.py`, which calls Bedrock several times
  (Curator + Reviewer training). If you want to skip the spend, run with
  `--layers infra,providers,routes,services`.
- The `routes` layer is purely GET — no side effects.
