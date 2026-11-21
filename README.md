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
  verified, posted **today** (America/Chicago), and nothing suggests it is an
  older role with a refreshed date.
- **Review** (one low-priority digest, `REVIEW.md`): date unknown, country
  unknown, a different engineering discipline, page not verified, or the date
  can't be trusted as same-day:
  - dated yesterday but only appeared now;
  - posted while the company's site couldn't be read (catch-up after an outage);
  - a term change (e.g. Spring to Summer) where the old title wasn't on the
    previous read;
  - a SuccessFactors job ID lower than roles already recorded days ago (that
    system re-dates old postings to "today", usually around 3 am CT);
  - the same title as a posting that came down earlier (a repost).
- **Skipped**: older than the window, outside the US, closed/error page, or not
  a relevant title.

Every decision is logged in `state/alerts.jsonl`. `state/seen.json` keeps, per
requisition, when it was first seen, the latest and the earliest employer date
(`posted_raw`, `posted_first`), and `redated_seen` when the employer moved the
date forward.

The alert window is set by the repository variable `ALERT_MODE`
(Settings → Secrets and variables → Actions → Variables):
`today` (default), `last_48_hours` or `newly_discovered`.
Days are counted in America/Chicago.

Optional secret `HANDLED_IDS`: requisition IDs you already applied to or
skipped (comma separated). They are never alerted again. Keep this in the
secret, not in the repo.

Tests: `python3 -m unittest discover -s tests`

## How it runs

`Internship watch (continuous)` (`.github/workflows/loop.yml`) checks every company,
waits about 35 minutes, and repeats. Before GitHub's 6-hour job limit it starts a fresh
copy of itself. GitHub's own scheduler proved unreliable (3 to 4 runs a day), so the
scheduled `Internship watch` workflow is now only a watchdog that restarts the
continuous one if it stops. To stop everything: Actions tab, cancel the running
continuous workflow, then disable both workflows.
