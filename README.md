# Maximize 1031 · Intake workspace

A standalone, branded website with a Python backend. Created separately from
`../M1031-intake-chat`; the original demo is not modified or used at runtime.

## Start locally

Python 3.13+ recommended. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. No API key is required for **Rehearsal**.
Use `127.0.0.1` locally, not `0.0.0.0`, unless production authentication is enabled.

## Try the demonstration

1. Select **Rehearsal · saved results** and click **Run intake review**.
2. Explore Intake, Evidence, Drafts, and the source documents.
3. Click **Add next batch**. The closing-date conflict is resolved and the
   operating agreement establishes signing authority.
4. Inspect History to compare revisions. Download the Markdown review ZIP.
5. Reset to begin another presentation.

Rehearsal explicitly uses the original demo's reviewed output snapshots. It does
not call an AI model. Python validates and renders those outputs before displaying
them. Initial expected counts: 19 found, 5 external open, 1 internal open, 1 conflict.
Updated: 22 found, 1 external open, 1 internal open, 0 conflicts.

## Enable live AI

Add `OPENAI_API_KEY` to `.env` with your editor, then restart the server.
Do not put the key in code, the browser, GitHub, or a chat message.
Set `OPENAI_MODEL` to a model your API project can access that supports Responses,
hosted shell, and structured outputs. The default is the official documentation's
`gpt-6-astra` example; account access has not been verified.

Select **Live AI · fresh review**, run the review, then ask case questions.
Changing modes requires resetting the case so saved and live results do not mix.
An API failure is shown as an error; the app never substitutes rehearsal output
for a live run.

For each live review the backend:

1. Packages the adapted `skill/` directory and only the selected source records.
   Initial bundles cannot access the later batch. Neither bundle includes rehearsal
   outputs, local credentials, or other files from your Box workspace.
2. Creates an OpenAI hosted container with the inline skill, then calls Responses.
3. Instructs the model to read the skill, assess the evidence, draft emails, and
   run the bundled Python renderer/validator in that container.
4. Independently validates returned Markdown in server-side Python, then stores
   the revision in the session's SQLite record.
5. Requests container deletion. If cleanup fails, hosted expiry is the fallback.

AI-directed shell code runs only in the hosted sandbox; the web server runs
trusted, fixed application code. No outbound network policy is enabled for the
hosted container. Chat answers are read-only and use the current review plus
the currently available source records.

## Deploy on Render with GitHub

See **DEPLOY.md** for exact steps. `render.yaml` expects this directory to be the
root of a dedicated repository. This demo uses the public repository
https://github.com/Maximize1031/Demo1, as selected by its owner. Do not upload the parent Claude folder.

Production requires a demo password of at least 16 characters. The blueprint uses
a paid web instance and a persistent disk. Hosting is not yet provisioned.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The tests exercise evidence validation, phase boundaries, session isolation,
authentication, background jobs, revision history, reset, API error handling,
rate limits, and mocked API contracts. A successful live API run additionally
requires credentials and account permissions.

## Scope and operational limits

- Synthetic Rivera records only; no uploads, PDF extraction, email sending,
  Accruit submission, or external connectors.
- Shared demo password, isolated random browser sessions, one-day cookie expiry.
  This is not a production client identity system.
- One application process with a two-thread job executor and SQLite on a persistent
  disk. Do not use multiple workers or replicas with this setup.
- Jobs interrupted by a restart are marked failed; they are not automatically
  retried or resumed. In-flight API calls may still incur usage.
- Maximum four queued/running jobs globally; default 50 live calls per rolling
  24 hours. This is a request cap, not a dollar budget. API tokens and containers
  have their own charges.
- Up to 12 review revisions per session; reset clears that session's review data.
  Request counters remain. Expired session records are cleaned on server startup.
- Output checks validate structure, source names, and identifier patterns, not
  the truth of the model's conclusions or professional/legal sufficiency.
- Outputs are displayed as text using DOM APIs, not inserted as model-generated
  HTML. Downloads are Markdown, not executable code.
- Before real clients: add individual accounts, durable job processing,
  backups/retention, client-level authorization, and a reviewed ingestion design.

## References

- https://developers.openai.com/api/docs/guides/tools-skills
- https://developers.openai.com/api/docs/guides/tools-shell
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://render.com/docs/web-services
- https://render.com/docs/disks

`original-demo-sha256.json` records the original demo's file hashes at the start
of this build so its preservation can be verified.
