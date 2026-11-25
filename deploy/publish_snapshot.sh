#!/bin/bash
# publish_snapshot.sh — Push the latest results database (data/tracker.db) to
# GitHub so the public Streamlit dashboard updates itself.
#
# Runs as the last step of the daily pipeline (see install_vm.sh). Only the
# small results file is published, never the multi-GB raw positions.
# Needs a one-time GitHub token setup on the VM (see README_VM steps in chat).
set -e
cd "$(dirname "$0")/.."

git add -f data/tracker.db
if git diff --cached --quiet; then
  echo "publish: no changes to tracker.db"
  exit 0
fi
git -c user.name="ord-collector VM" -c user.email="ord-collector@users.noreply.github.com" \
    commit --quiet -m "Daily data update $(date -u +%Y-%m-%d)"
git pull --quiet --rebase origin main    # include any code pushed from the laptop
git push --quiet origin main
echo "publish: pushed tracker.db ($(du -h data/tracker.db | cut -f1))"
