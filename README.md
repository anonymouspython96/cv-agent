# CV Agent

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/emiliantimofei)

An agent that sends your CV as a job application by email, using the
[Resend](https://resend.com) HTTP API. Works behind firewalls that block
SMTP ports 25 / 465 / 587.

## Features

- Personalised application emails (text + HTML) with your CV attached as PDF
- Optional LLM-written opening paragraph (`--personalize`, needs `OPENAI_API_KEY`)
- De-duplication against an append-only `sent.jsonl` log — nobody gets emailed twice
- `--dry-run` writes the exact `.eml` files to `outbox/` without sending
- Randomised delays between sends to stay polite

## Quick start

```bash
git clone https://github.com/<you>/cv-agent.git
cd cv-agent
python -m venv .venv && source .venv/bin/activate
pip install resend python-dotenv

cp .env.example .env              # add your RESEND_API_KEY
cp profile.example.json profile.json   # fill in your details
cp contacts.example.csv contacts.csv   # add your targets
cp /path/to/your/CV.pdf cv/YourCV.pdf  # update CV_PATH in cv_agent.py if renamed

./run.sh --check
./run.sh --dry-run --limit=1
./run.sh --limit=1
