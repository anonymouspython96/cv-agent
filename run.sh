#!/usr/bin/env bash
# run.sh — wrapper for cv_agent.py
#
# Usage:
#   ./run.sh --manual              Print the user manual
#   ./run.sh --check               Validate files and structure
#   ./run.sh --dry-run             Compose emails without sending
#   ./run.sh --dry-run --limit=3   Same, for 3 emails
#   ./run.sh --limit=5             Send to 5 contacts
#   ./run.sh --personalize --limit=5
#   ./run.sh -v                    Verbose logging
#
# Any argument is forwarded to cv_agent.py.

cd "$(dirname "$0")" || exit 1

# Activate virtualenv if present
if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Load .env if present (set -a exports every variable)
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env 2>/dev/null || true
    set +a
fi

exec python cv_agent.py "$@"