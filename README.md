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
