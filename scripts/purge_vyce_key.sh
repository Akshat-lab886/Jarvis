#!/usr/bin/env bash
# purge_vyce_key.sh
# =====================================================================
# SECURITY: purge the exposed Vyce API key from Jarvis git history.
#
# The key  sk-3584dfb613f16ff98b2915ee1b7330a19c4f9367e25ab452
# was committed in commits 85d67a6 and ea3d40e (before the BYOK fix
# in 8b5046f). It is NOT in the working tree or .env.example — only in
# HISTORY. Rotate the key first (see step 0), then run THIS script:
#
#   0. Rotate: revoke  on https://vyceai.com and issue a NEW key.
#   1. brew install git-filter-repo   (macOS)  OR  pip install git-filter-repo
#   2. ./scripts/purge_vyce_key.sh
#   3. git push origin main --force-with-lease
#   4. Ask every collaborator to re-clone / rebase.
#
# This script does NOT push — review the --replace-text output first.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

KEY="${VYCE_KEY:-sk-3584dfb613f16ff98b2915ee1b7330a19c4f9367e25ab452}"
FILTER_FILE="$(mktemp)"

# git-filter-repo --replace-text rules: mask the key, keep length-ish shape.
cat > "$FILTER_FILE" <<EOF
regex:${KEY}=>>>REDACTED-VYCE-KEY-ROTATE-ME<<<
EOF

echo "=== BEFORE (history contains key) ==="
git log --oneline -S "${KEY}" -- . | head -4 || true

git filter-repo --replace-text "$FILTER_FILE" --force

rm -f "$FILTER_FILE"
echo
echo "=== AFTER (history scrubbed) ==="
git log -p --all | grep -c "${KEY}" || echo "0 — key fully purged"
echo
echo "Now rotate the key on https://vyceai.com and re-issue a new one,"
echo "then:  git push origin main --force-with-lease"
echo "(Force-push will rewrite history for everyone — coordinate with your team.)"
