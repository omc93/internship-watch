#!/usr/bin/env bash
# Commit and push the checker's state. Retries if another pass pushed in between.
git config user.name "internship-watch"
git config user.email "internship-watch@users.noreply.github.com"
date +%s > state/last_run
git add state STATUS.md
git add ALERTS.md REVIEW.md 2>/dev/null || true
git diff --cached --quiet && exit 0
git commit -q -m "Update seen postings"
for i in 1 2 3; do
  git push -q && exit 0
  git pull -q --rebase origin main || { git rebase --abort; echo "Rebase conflict"; exit 0; }
done
echo "Push failed; next run will retry."
