# OPTIC BOT

Django backend that polls a shared Outlook mailbox every 5 minutes, extracts
order data from attachments (PDF/Word/Excel/CSV/image) with an LLM, matches
the customer to an OE code via a pgvector RAG agent, and exposes REST APIs
for a human reviewer to approve/edit low-confidence orders.

No frontend here - APIs only. See `PLAN.md` for the full architecture,
design decisions, and the Appendix A pgvector table spec.

---

## Prerequisites

- Python 3.11+ (developed and tested on 3.14)
- Git Bash / PowerShell (Windows) or any POSIX shell

---

## 1. Set up the virtual environment

From the project root (`TestBot/`):

**Git Bash / macOS / Linux:**
```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# source .venv/bin/activate        # macOS / Linux
```

**PowerShell:**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

You'll know it worked when your prompt shows `(.venv)`. Do this every time
you open a new terminal to work on the project - the steps below all assume
the venv is active.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Configure `.env`

```bash
cp .env.example .env          # Git Bash / macOS / Linux
copy .env.example .env        # PowerShell / cmd
```

The defaults in `.env.example` (`DB_ENGINE=sqlite`, `RUN_SCHEDULER=False`,
everything else blank) are enough to run and test the whole app with **zero
credentials** - see "Running without any credentials" below. Fill in the
real values (Outlook, AWS, LLM, OE RAG agent) when you have them; nothing
else needs to change.

## 4. Set up the database

```bash
python manage.py migrate
```

This creates `db.sqlite3` locally. To use Postgres instead, set
`DB_ENGINE=postgres` and fill in `DB_NAME`/`DB_USER`/`DB_PASSWORD`/
`DB_HOST`/`DB_PORT` in `.env`, then run `migrate` again.

## 5. Run the server

```bash
python manage.py runserver
```

The API is now at `http://127.0.0.1:8000/api/`. Try:
```bash
curl http://127.0.0.1:8000/api/health/
curl http://127.0.0.1:8000/api/config/
curl http://127.0.0.1:8000/api/orders/
```

Every endpoint also works in the browser as DRF's browsable API (open
`http://127.0.0.1:8000/api/orders/` directly) - useful since there's no
frontend yet.

---

## Running without any credentials

The app is designed to boot and be testable with an empty `.env`. With no
Outlook/AWS/LLM/RAG values set:
- `GET /api/health/` still returns 200, listing each dependency as
  `not_configured: <missing keys>`.
- `POST /api/poll/` returns a clean `400` naming exactly which `.env` keys
  are missing, instead of crashing.
- All the review endpoints (`orders/`, `fields/`, `approve/`, `reject/`,
  `audit/`) work fully against whatever is already in the database.

## Polling Outlook every 5 minutes

Set `RUN_SCHEDULER=True` in `.env` and fill in the `MS_*` / `MAILBOX_*`
values, then run the server normally:
```bash
python manage.py runserver
```
A background job polls the mailbox every `MAIL_POLL_MINUTES` (default 5)
and once immediately on startup. To trigger a poll on demand instead of
waiting:
```bash
curl -X POST http://127.0.0.1:8000/api/poll/
```

---

## Common commands

| Task | Command |
|---|---|
| Activate the venv | `source .venv/Scripts/activate` (Git Bash) |
| Install/update deps | `pip install -r requirements.txt` |
| Make migrations after a model change | `python manage.py makemigrations optic_bot` |
| Apply migrations | `python manage.py migrate` |
| Run the dev server | `python manage.py runserver` |
| Django system check | `python manage.py check` |
| Open a Python shell with the app loaded | `python manage.py shell` |
| Deactivate the venv | `deactivate` |

---

## Project layout

```
optic_bot/          the app - start reading at views.py
  views.py           every API endpoint
  services.py        Outlook, S3, LLM, OE lookup, the pipeline, CSV export
  models.py           Order + AuditLog (2 tables)
  scheduler.py         5-minute poll
  urls.py
opticbot/            Django project config (settings.py, urls.py)
prompts/
  order_extraction.txt   the LLM prompt (editable without a code change)
.env                  your local secrets/config (gitignored, never commit)
.env.example          template listing every key the code reads
PLAN.md               full architecture, design decisions, verification steps
```

See `PLAN.md` for everything else: the API reference table, the pgvector OE
table spec (Appendix A), and the Phase 2 backlog.
