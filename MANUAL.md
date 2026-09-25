# CV Agent — User Manual

An agent that sends your CV as a job application by email using the
**Resend** API over HTTPS. It works even behind firewalls that block
SMTP ports 25 / 465 / 587.

---

## 1. Required files

| File | Description |
|---|---|
| `profile.json` | Your details (name, email, phone, location, summary) |
| `contacts.csv` | List of companies / roles / emails to contact |
| `cv/yourCV.pdf` | Your CV in PDF format |
| `.env` | Resend API key and sender settings |
| `sent.jsonl` | Created automatically. Log of every send. |
| `outbox/` | Created automatically. Holds dry-run email files. |

---

## 2. Variables in `.env`

| Variable | Required | Default | Notes |
|---|---|---|---|
| `RESEND_API_KEY` | yes | — | Starts with `re_...` |
| `SENDER_NAME` | no | `your name` | Display name |
| `SENDER_EMAIL` | no | `onboarding@resend.dev` | Must be verified on Resend |
| `SUBJECT_TEMPLATE` | no | `Application — {role} — {sender_name}` | Placeholders: `{role}`, `{company}`, `{sender_name}` |
| `MIN_DELAY_SECONDS` | no | `45` | Minimum pause between emails |
| `MAX_DELAY_SECONDS` | no | `120` | Maximum pause between emails |
| `MAX_PER_RUN` | no | `25` | Emails per run |
| `OPENAI_API_KEY` | no | — | Only needed with `--personalize` |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | LLM model |

---

## 3. Syntax
./run.sh [--dry-run] [--limit=N] [--check] [--personalize] [-v] [--manual]

All arguments are optional and can be combined. `--limit=5` and
`--limit 5` are equivalent.

---

## 4. Commands

### `./run.sh --manual`
Prints this manual and exits.

### `./run.sh --check`
Validates that `profile.json`, `contacts.csv` and the CV exist.
Sends nothing, touches nothing online.

### `./run.sh --dry-run`
Composes the emails and writes them to `outbox/` without sending.
Does not consume Resend quota. Does not write to `sent.jsonl`.

### `./run.sh --dry-run --limit=3`
Same as above, but only for the first 3 not-yet-contacted targets.
Use it to inspect subject, body and attachment.

### `./run.sh --limit=5`
Actually sends to 5 contacts.

### `./run.sh --limit=1`
Sends to 1 contact. Use it for the very first real test to yourself.

### `./run.sh --personalize --limit=5`
Same as above, but asks an LLM to write a tailored opening line.
Requires `OPENAI_API_KEY` in `.env`.

### `./run.sh -v`
Verbose (debug) logging. Combinable with everything.

### `./run.sh`
No arguments: uses `MAX_PER_RUN` from `.env` (default 25).

---

## 5. Recommended workflow

```bash
# 1. Validate structure
./run.sh --check

# 2. Build 1 email, do not send
./run.sh --dry-run --limit=1
less outbox/*.eml        # check subject, body, attachment

# 3. If it looks right, send 1 real email to yourself
#    (first add your own address to the top of contacts.csv)
./run.sh --limit=1

# 4. Check inbox and spam folder

# 5. First real batch
./run.sh --limit=5

# 6. Steady state
./run.sh --limit=10
