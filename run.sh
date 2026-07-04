#!/usr/bin/env bash
# run.sh — run bonzai.py on the droplet and pull the report back locally.
set -euo pipefail

DROPLET="root@161.35.122.12"
REMOTE_DIR="/root/bonzai"
REMOTE_OUT="/tmp"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

log="$(mktemp)"
trap 'rm -f "$log"' EXIT

echo "→ running bonzai.py on $DROPLET"
ssh "$DROPLET" "cd $REMOTE_DIR && python3 bonzai.py $REMOTE_OUT" | tee "$log"

report_path="$(awk '/^Wrote: /{print $2}' "$log")"
if [[ -z "$report_path" ]]; then
  echo "✗ could not find 'Wrote:' line in droplet output" >&2
  exit 1
fi

echo
echo "→ copying $report_path → $LOCAL_DIR/"
scp "$DROPLET:$report_path" "$LOCAL_DIR/"

echo "→ removing $report_path from droplet"
ssh "$DROPLET" "rm -f '$report_path'"

echo "✓ saved: $LOCAL_DIR/$(basename "$report_path")"
