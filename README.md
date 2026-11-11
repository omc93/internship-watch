# Internship watch

Checks 132 companies' own job systems every hour and sends new internship
postings to your phone through ntfy.

- `companies.py`: the company list. Add or remove companies here.
- `watch.py`: the checker.
- `STATUS.md`: which companies are working and how many internships each has open.
- `state/`: what has already been seen (updated automatically).

The first time a company is checked successfully, its current postings are
recorded without alerts. After that, only postings that weren't there before
trigger an alert. Postings clearly located outside the US are ignored.

## Setup

1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   name `NTFY_TOPIC`, value = your ntfy topic name.
2. **Actions** tab → **Internship watch** → **Run workflow** → tick
   *Send a test notification only* → **Run workflow**. Your phone should buzz.
3. Run it again without the tick to do the first full check.

## What counts as an alert

A posting is **new** when its requisition ID has not been seen before (discovery).
That is different from when the employer **posted** it, so the two are tracked
separately. Each newly found engineering posting is opened on the employer's
site, then sorted:

- **Alert** (phone notification, `ALERTS.md`): US role, strong or good fit, page
  verified, and the employer-reported date is inside the alert window.
- **Review** (one low-priority digest, `REVIEW.md`): date unknown, country
  unknown, a different engineering discipline, or the page could not be verified.
- **Skipped**: older than the window, outside the US, closed/error page, or not
  a relevant title.

Every decision is logged in `state/alerts.jsonl`.

The alert window is set by the repository variable `ALERT_MODE`
(Settings → Secrets and variables → Actions → Variables):
`today`, `last_48_hours` (default) or `newly_discovered`.
Dates are employer-reported: a refreshed posting can show today's date on an
older role. Days are counted in America/Chicago.

Optional secret `HANDLED_IDS`: requisition IDs you already applied to or
skipped (comma separated). They are never alerted again. Keep this in the
secret, not in the repo.

Tests: `python3 -m unittest discover -s tests`
