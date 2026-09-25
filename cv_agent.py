#!/usr/bin/env python3
"""
cv_agent.py — an agent that sends your CV as a job application over the internet.

Features
--------
* Loads a structured profile (profile.json) built from your CV
* Loads target companies/roles from contacts.csv
* Composes a personalised application email (text + HTML) and attaches your CV PDF
* Optional LLM-written opening paragraph (--personalize, needs OPENAI_API_KEY)
* Sends over the Resend HTTP API (no SMTP, works behind firewalls)
* De-duplicates against an append-only JSONL log so nobody gets emailed twice
* --dry-run writes the exact emails to outbox/ without sending anything

Usage
-----
    cp .env.example .env      # fill in RESEND_API_KEY
    python cv_agent.py --check
    python cv_agent.py --dry-run
    python cv_agent.py --limit 10
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import resend
from resend.exceptions import ResendError
from dotenv import load_dotenv
load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.json"
CONTACTS_PATH = ROOT / "contacts.csv"
CV_PATH = ROOT / "cv" / "EmilianCV.pdf"
SENT_LOG = ROOT / "sent.jsonl"
OUTBOX = ROOT / "outbox"
TEMPLATE_TXT = ROOT / "templates" / "email.txt"
TEMPLATE_HTML = ROOT / "templates" / "email.html"

log = logging.getLogger("cv_agent")


@dataclass
class Config:
    resend_api_key: str
    sender_name: str
    sender_email: str
    subject_template: str
    min_delay: float
    max_delay: float
    max_per_run: int
    personalize: bool
    openai_model: str

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        missing = [v for v in ("RESEND_API_KEY",) if not os.getenv(v)]
        if missing and not args.dry_run:
            raise SystemExit(
                f"Missing environment variables: {', '.join(missing)}. "
                "Add them to .env."
            )
        return cls(
            resend_api_key=os.getenv("RESEND_API_KEY", ""),
            sender_name=os.getenv("SENDER_NAME", "Emilian Timofei"),
            sender_email=os.getenv("SENDER_EMAIL", "onboarding@resend.dev"),
            subject_template=os.getenv(
                "SUBJECT_TEMPLATE", "Application — {role} — {sender_name}"
            ),
            min_delay=float(os.getenv("MIN_DELAY_SECONDS", "45")),
            max_delay=float(os.getenv("MAX_DELAY_SECONDS", "120")),
            max_per_run=int(os.getenv("MAX_PER_RUN", "25")),
            personalize=args.personalize,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )


@dataclass
class Contact:
    company: str
    role: str
    email: str
    contact_name: str = ""
    notes: str = ""

    @property
    def key(self) -> str:
        return self.email.strip().lower()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        raise SystemExit(f"Missing {PROFILE_PATH}")
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_contacts() -> list[Contact]:
    if not CONTACTS_PATH.exists():
        raise SystemExit(f"Missing {CONTACTS_PATH}")
    out: list[Contact] = []
    with CONTACTS_PATH.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            email = (row.get("email") or "").strip()
            if not email or "@" not in email:
                log.warning("Skipping row with bad email: %s", row)
                continue
            out.append(
                Contact(
                    company=(row.get("company") or "").strip(),
                    role=(row.get("role") or "").strip(),
                    email=email,
                    contact_name=(row.get("contact_name") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return out


def load_sent() -> set[str]:
    if not SENT_LOG.exists():
        return set()
    sent: set[str] = set()
    for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sent.add(json.loads(line)["key"])
        except (json.JSONDecodeError, KeyError):
            continue
    return sent


def record_sent(contact: Contact, status: str, detail: str = "") -> None:
    rec = {
        "key": contact.key,
        "email": contact.email,
        "company": contact.company,
        "role": contact.role,
        "status": status,
        "detail": detail,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with SENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------- #
# Personalisation (optional LLM step)
# --------------------------------------------------------------------------- #

PERSONALIZE_PROMPT = """You are helping {sender_name} write the opening paragraph of a job \
application email.

Company: {company}
Role: {role}
Extra notes about this target: {notes}

Candidate background: {summary}
Candidate headline: {headline}

Write ONLY the paragraph (2-3 sentences). No greeting, no sign-off, no bullet points.
Be concrete and specific to the company/role. No flattery, no buzzwords, no "I am
writing to express my interest". Sound like a competent engineer, not a cover letter."""


def llm_paragraph(profile: dict, contact: Contact, cfg: Config) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("--personalize set but OPENAI_API_KEY missing; skipping LLM step.")
        return ""

    prompt = PERSONALIZE_PROMPT.format(
        sender_name=profile["name"],
        company=contact.company or "the company",
        role=contact.role or "the role",
        notes=contact.notes or "(none)",
        summary=profile.get("summary", ""),
        headline=profile.get("headline", ""),
    )
    payload = json.dumps(
        {
            "model": cfg.openai_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 200,
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
        log.warning("LLM personalisation failed (%s); using template only.", exc)
        return ""


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #

DEFAULT_TEXT = """\
Dear {contact_name},

{personal_note}

I am applying for {role} at {company}. I am an IT support and implementation
specialist based in Malta, working at the intersection of infrastructure support,
web development and AI-assisted delivery.

What I bring:
- Infrastructure: Windows Server, Active Directory, Proxmox, Linux, DHCP/DNS,
  subnets and end-to-end connectivity troubleshooting.
- Support: ticketing systems, issue categorisation, escalation workflows and
  multilingual customer communication (English, Romanian, Italian, Portuguese).
- Delivery: front-end builds from UI/UX mock-ups in HTML, CSS, SCSS, Bootstrap,
  JavaScript and Vue, plus Python scripting and SQL.
- AI-augmented engineering: I use LLMs as a genuine tool for generating and
  debugging code, writing tests and documentation, and planning deployments —
  always verified and owned by me before anything ships.

My CV is attached. I would welcome the chance to discuss how I can help {company}.

Best regards,
{sender_name}
{headline}
{email} · {phone} · {location}
"""

DEFAULT_HTML = """\
<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#222">
<p>Dear {contact_name},</p>
<p>{personal_note}</p>
<p>I am applying for <strong>{role}</strong> at <strong>{company}</strong>. I am an IT
support and implementation specialist based in Malta, working at the intersection of
infrastructure support, web development and AI-assisted delivery.</p>
<p><strong>What I bring:</strong></p>
<ul>
  <li><strong>Infrastructure:</strong> Windows Server, Active Directory, Proxmox, Linux,
      DHCP/DNS, subnets and end-to-end connectivity troubleshooting.</li>
  <li><strong>Support:</strong> ticketing systems, issue categorisation, escalation
      workflows and multilingual customer communication (EN / RO / IT / PT).</li>
  <li><strong>Delivery:</strong> front-end builds from UI/UX mock-ups in HTML, CSS, SCSS,
      Bootstrap, JavaScript and Vue, plus Python scripting and SQL.</li>
  <li><strong>AI-augmented engineering:</strong> LLMs used for generating and debugging
      code, tests, documentation and deployment planning — always verified and owned by
      me before anything ships.</li>
</ul>
<p>My CV is attached. I would welcome the chance to discuss how I can help {company}.</p>
<p>Best regards,<br>
<strong>{sender_name}</strong><br>
{headline}<br>
{email} · {phone} · {location}</p>
</body></html>
"""


def build_fields(profile: dict, contact: Contact, cfg: Config, personal_note: str) -> dict:
    return {
        "sender_name": profile["name"],
        "headline": profile.get("headline", ""),
        "email": profile["email"],
        "phone": profile.get("phone", ""),
        "location": profile.get("location", ""),
        "company": contact.company or "your company",
        "role": contact.role or "the advertised role",
        "contact_name": contact.contact_name or "Hiring Team",
        "personal_note": personal_note,
    }


def compose(profile: dict, contact: Contact, cfg: Config) -> EmailMessage:
    personal_note = (
        llm_paragraph(profile, contact, cfg)
        if cfg.personalize
        else "I am reaching out regarding an opportunity at your company."
    )
    fields = build_fields(profile, contact, cfg, personal_note)

    def _read_template(path: Path, default: str) -> str:
        if not path.exists():
            return default
        raw = path.read_text(encoding="utf-8").strip()
        return raw if raw else default

    text_tpl = _read_template(TEMPLATE_TXT, DEFAULT_TEXT)
    html_tpl = _read_template(TEMPLATE_HTML, DEFAULT_HTML)

    subject = cfg.subject_template.format(**fields)

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
    msg["To"] = formataddr((contact.contact_name, contact.email)) if contact.contact_name else contact.email
    msg["Reply-To"] = cfg.sender_email
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.sender_email.split("@")[-1])

    msg.set_content(text_tpl.format(**fields))
    msg.add_alternative(html_tpl.format(**fields), subtype="html")

    if CV_PATH.exists():
        msg.add_attachment(
            CV_PATH.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=CV_PATH.name,
        )
    else:
        log.warning("CV not found at %s — sending without attachment.", CV_PATH)

    return msg


# --------------------------------------------------------------------------- #
# Delivery (Resend HTTP API)
# --------------------------------------------------------------------------- #

def send(msg: EmailMessage, cfg: Config, attempts: int = 3) -> None:
    """Send an email via the Resend HTTP API."""
    resend.api_key = cfg.resend_api_key

    # Extract plain-text and HTML parts
    text_body = ""
    html_body = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "text/plain":
            text_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
        elif content_type == "text/html":
            html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")

    # Extract attachments
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if filename:
            attachments.append({
                "filename": filename,
                "content": list(part.get_payload(decode=True)),
            })

    params: resend.Emails.SendParams = {
        "from": f"{cfg.sender_name} <{cfg.sender_email}>",
        "to": [msg["To"]],
        "subject": msg["Subject"],
        "text": text_body,
        "html": html_body,
    }
    if attachments:
        params["attachments"] = attachments

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            email = resend.Emails.send(params)
            log.debug("Resend response: %s", email)
            return
        except (ResendError, OSError) as exc:
            last_exc = exc
            wait = 2 ** attempt
            log.warning("Send failed (attempt %d/%d): %s — retrying in %ds",
                        attempt, attempts, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Send failed after {attempts} attempts: {last_exc}")


def write_to_outbox(msg: EmailMessage, contact: Contact) -> Path:
    OUTBOX.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in contact.email)
    path = OUTBOX / f"{safe}.eml"
    path.write_bytes(bytes(msg))
    return path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send your CV as a job application.",
        epilog="Full user manual: ./run.sh --manual",
    )
    p.add_argument("--manual", action="store_true",
                   help="Print the full user manual and exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compose and save to outbox/ without sending.")
    p.add_argument("--check", action="store_true",
                   help="Validate profile, contacts and CV, then exit.")
    p.add_argument("--limit", type=int, default=None,
                   help="Max emails this run (overrides MAX_PER_RUN).")
    p.add_argument("--personalize", action="store_true",
                   help="Use an LLM to write the opening paragraph.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    profile = load_profile()
    contacts = load_contacts()
    cfg = Config.from_env(args)
    sent = load_sent()

    log.info("Profile: %s — %s", profile["name"], profile.get("headline", ""))
    log.info("Contacts: %d loaded, %d already contacted", len(contacts), len(sent))
    log.info("CV: %s", "found" if CV_PATH.exists() else f"MISSING ({CV_PATH})")

    if args.check:
        pending = [c for c in contacts if c.key not in sent]
        log.info("Check OK. %d pending contact(s).", len(pending))
        for label, path in (("text", TEMPLATE_TXT), ("html", TEMPLATE_HTML)):
            if path.exists() and not path.read_text(encoding="utf-8").strip():
                log.warning("Template %s is empty — DEFAULT will be used.", path)
        return 0

    limit = args.limit if args.limit is not None else cfg.max_per_run
    queue = [c for c in contacts if c.key not in sent][:limit]

    if not queue:
        log.info("Nothing to do — everyone on the list has been contacted.")
        return 0

    log.info("Queue: %d contact(s)%s", len(queue), " (DRY RUN)" if args.dry_run else "")

    ok = failed = 0
    for i, contact in enumerate(queue, start=1):
        label = f"[{i}/{len(queue)}] {contact.company or '?'} <{contact.email}>"
        try:
            msg = compose(profile, contact, cfg)
            if args.dry_run:
                path = write_to_outbox(msg, contact)
                log.info("%s -> dry-run, saved %s", label, path.name)
                ok += 1
            else:
                send(msg, cfg)
                record_sent(contact, "sent")
                log.info("%s -> sent", label)
                ok += 1
        except Exception as exc:  # noqa: BLE001 — keep the run going
            log.error("%s -> FAILED: %s", label, exc)
            record_sent(contact, "failed", str(exc))
            failed += 1

        if i < len(queue) and not args.dry_run:
            delay = random.uniform(cfg.min_delay, cfg.max_delay)
            log.debug("Sleeping %.1fs", delay)
            time.sleep(delay)

    log.info("Done. sent=%d failed=%d", ok, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())