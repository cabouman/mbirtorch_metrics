#!/usr/bin/env bash
# create_token.sh — store the GitHub personal access token the regression harness uses to push to
# mbirtorch_metrics (one-time per node).  See create_token_instructions.md (same dir) for which kind
# of token works and how to make it; the repository's owner decides that, so read it before
# generating one.  Writes a git credential-store file (chmod 600), holding one line:
#   https://<user>:<token>@github.com
set -euo pipefail
# Keep an interactive terminal open on a nonzero exit so the error stays visible.
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTRUCTIONS="$HERE/create_token_instructions.md"
CRED_FILE="${1:-$HOME/.config/mbirtorch/metrics_credentials}"

echo "Store a GitHub personal access token so the regression harness can push to mbirtorch_metrics."
echo "Credential file: $CRED_FILE"
echo

# Read the PAT (hidden).  Pressing Enter with NO token opens the instructions, then re-prompts —
# so you can pull up the how-to right here at the 'Paste PAT' step if you don't have one ready.
read -rsp "Paste PAT (hidden), or press Enter alone to view the instructions first: " PAT
echo
if [ -z "${PAT:-}" ]; then
  if [ -f "$INSTRUCTIONS" ]; then
    if command -v less >/dev/null 2>&1; then less "$INSTRUCTIONS"; else cat "$INSTRUCTIONS"; fi
  else
    echo "  (instructions not found at $INSTRUCTIONS)"
  fi
  read -rsp "Paste PAT (hidden): " PAT
  echo
fi
[ -n "${PAT:-}" ] || { echo "No token entered — nothing written."; exit 1; }

read -rp "GitHub username: " GH_USER
[ -n "${GH_USER:-}" ] || { echo "Username required — nothing written."; exit 1; }

mkdir -p "$(dirname "$CRED_FILE")"
chmod 700 "$(dirname "$CRED_FILE")" 2>/dev/null || true
( umask 077; printf 'https://%s:%s@github.com\n' "$GH_USER" "$PAT" >"$CRED_FILE" )
chmod 600 "$CRED_FILE"
unset PAT

echo
echo "Wrote $CRED_FILE (chmod 600)."
echo "regression.env defaults TOKEN_FILE to this path, so the harness will use it."
echo "Verify from any mbirtorch_metrics clone:"
echo "  GIT_TERMINAL_PROMPT=0 git -c credential.helper=\"store --file=$CRED_FILE\" push --dry-run"
