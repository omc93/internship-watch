"""Regression tests. Run with:  python3 -m unittest discover -s tests

No network: every website is faked.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import triage  # noqa: E402
import watch  # noqa: E402

NOW = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)      # Nov 10, 4:00 PM in Chicago


class Dates(unittest.TestCase):
    def test_unseen_30_day_old_role_is_not_recent(self):
        for mode in ("today", "last_48_hours"):
            self.assertEqual(triage.recency("Posted 30+ Days Ago", mode, NOW), "old")
            self.assertEqual(triage.recency("2026-10-11", mode, NOW), "old")

    def test_no_date_is_unknown_not_recent(self):
        self.assertEqual(triage.recency("", "today", NOW), "unknown")
        self.assertEqual(triage.recency("", "last_48_hours", NOW), "unknown")
        p = {"title": "Mechanical Engineering Intern", "location": "Austin, TX", "posted": "", "id": "1"}
        self.assertEqual(triage.classify(p, "today", NOW), ("review", ["posted date unknown"]))

    def test_newly_discovered_mode_ignores_dates(self):
        self.assertEqual(triage.recency("", "newly_discovered", NOW), "recent")

    def test_utc_after_midnight_is_still_previous_day_in_chicago(self):
        late = datetime(2026, 11, 11, 5, 30, tzinfo=timezone.utc)   # Nov 10, 11:30 PM Chicago
        d = triage.parse_posted("2026-11-11T02:00:00Z", late)       # Nov 10, 8:00 PM Chicago
        self.assertEqual(d["day"].isoformat(), "2026-11-10")
        self.assertEqual(triage.recency("2026-11-11T02:00:00Z", "today", late), "recent")

    def test_offset_is_kept_and_date_only_gets_no_fake_time(self):
        d = triage.parse_posted("2026-11-10T15:46:00-06:00", NOW)
        self.assertEqual(d["precision"], "timestamp")
        self.assertEqual(d["when"].astimezone(timezone.utc).hour, 21)
        for raw in ("2026-11-10", "2026-11-10T00:00:00.000+0000", "Nov 10, 2026"):
            d = triage.parse_posted(raw, NOW)
            self.assertIsNone(d["when"], raw)
            self.assertEqual(d["day"].isoformat(), "2026-11-10")

    def test_date_only_in_48_hour_mode_means_today_or_yesterday(self):
        self.assertEqual(triage.recency("2026-11-09", "last_48_hours", NOW), "recent")
        self.assertEqual(triage.recency("2026-11-08", "last_48_hours", NOW), "old")
        self.assertEqual(triage.recency("2026-11-09", "today", NOW), "old")


class Country(unittest.TestCase):
    def test_country_handling(self):
        cases = [("St-Laurent, QC, CAN", "", "non-US"), ("Newmarket, ON, Canada", "", "non-US"),
                 ("Newmarket, ON", "", "non-US"), ("", "", "unknown"),
                 ("Los Angeles, CA, US", "", "US"), ("Costa Mesa, CA", "", "US"),
                 ("Remote - Canada", "", "non-US"), ("Remote", "", "unknown"),
                 ("Toronto", "ca", "non-US"), ("Milton, ON, CA, L9T 0M1", "", "non-US"),
                 ("Milton-ON-L9T-0M1", "", "non-US"), ("Portland, OR", "", "US"), ("Austin", "US", "US"), ("3 Locations", "", "unknown")]
        for loc, country, want in cases:
            self.assertEqual(triage.country_of(loc, country), want, (loc, country))

    def test_unknown_country_goes_to_review_and_foreign_is_skipped(self):
        base = {"title": "Mechanical Engineering Intern", "posted": "Posted Today", "id": "1"}
        self.assertEqual(triage.classify(dict(base, location=""), "today", NOW)[0], "review")
        self.assertEqual(triage.classify(dict(base, location="St-Laurent, QC, CAN"), "today", NOW)[0], "skip")
        self.assertEqual(triage.classify(dict(base, location="Cedar Park, TX"), "today", NOW)[0], "alert")


class Relevance(unittest.TestCase):
    def test_labels(self):
        cases = [("Mechanical Engineering Intern (Summer 2027)", "strong fit"),
                 ("Internship - Propulsion Engineering - Summer 2027", "strong fit"),
                 ("Manufacturing Engineering Intern Summer 2027", "fit"),
                 ("Social Media Engineering Intern (Winter/Spring 2027)", "excluded"),
                 ("Summer 2027 - Process Engineering Intern - MS/PhD", "excluded"),
                 ("Facilities Water Treatment Technician Intern", "excluded"),
                 ("Structural Engineering Intern", "needs review"),
                 ("5G Network Intelligence and Automation Intern", "excluded"),
                 ("Electrical Engineering Intern", "needs review"),
                 ("Supply Chain Intern", "excluded")]
        for title, want in cases:
            self.assertEqual(triage.relevance(title)[0], want, title)

    def test_requirement_flags_are_labels_only(self):
        flags = triage.requirement_flags("Must be a U.S. citizen. Relocation assistance is not provided. "
                                         "Graduation date between December 2027 and June 2028.")
        self.assertIn("citizenship/ITAR stated", flags)
        self.assertTrue(any(f.startswith("graduation:") for f in flags))
        self.assertIn("housing/relocation: unstated", triage.requirement_flags("Design brackets."))

    def test_handled_requisition_is_suppressed_but_other_season_is_kept(self):
        base = {"title": "Mechanical Engineering Intern", "location": "Austin, TX", "posted": "Posted Today"}
        self.assertEqual(triage.classify(dict(base, id="JR1001"), "today", NOW, {"JR1001"})[0], "skip")
        self.assertEqual(triage.classify(dict(base, id="JR1002"), "today", NOW, {"JR1001"})[0], "alert")


class Links(unittest.TestCase):
    def test_job_card_title_is_the_heading_only(self):
        card = ('<a href="/job/camden/intern/1"><h2>Intern, Logistics (Camden, AR)</h2>'
                '<span>Manufacturing|Co Op/Intern</span><span>Camden, AR</span></a>')
        found = watch.links_from(card, "https://careers.example.com", r"/job/")
        self.assertEqual(found[0]["title"], "Intern, Logistics (Camden, AR)")
        self.assertEqual(triage.relevance(found[0]["title"])[0], "excluded")


class Pages(unittest.TestCase):
    def test_200_error_page_is_not_an_open_job(self):
        err = "<html><title>Error</title><body>An error has occurred. The page you requested...</body></html>"
        self.assertNotEqual(triage.page_status(err, "Intern, Electromechanical Design", "15387"), "open")
        ok = "<html><body><h1>Intern, Electromechanical Design</h1> Req 15387 <a>Apply now</a></body></html>"
        self.assertEqual(triage.page_status(ok, "Intern, Electromechanical Design", "15387"), "open")


def run_flow(test, http_fake, companies, runs=1, mode="last_48_hours"):
    """Run watch.main() against fake sites. Returns (notifications, state dir)."""
    work = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, work, True)
    sent = []
    saved = {k: getattr(watch, k) for k in ("http", "notify", "COMPANIES", "ROOT", "STATE_DIR", "SEEN_FILE",
                                             "RESOLVED_FILE", "STATUS_FILE", "ALERTS_FILE", "REVIEW_FILE",
                                             "AUDIT_FILE", "ALERT_MODE")}
    test.addCleanup(lambda: [setattr(watch, k, v) for k, v in saved.items()])
    watch.http = http_fake
    watch.notify = lambda title, msg, click=None, **k: sent.append((title, msg, click))
    watch.COMPANIES = companies
    watch.ROOT, watch.STATE_DIR = work, os.path.join(work, "state")
    watch.SEEN_FILE = os.path.join(watch.STATE_DIR, "seen.json")
    watch.RESOLVED_FILE = os.path.join(watch.STATE_DIR, "resolved.json")
    watch.STATUS_FILE = os.path.join(work, "STATUS.md")
    watch.ALERTS_FILE = os.path.join(work, "ALERTS.md")
    watch.REVIEW_FILE = os.path.join(work, "REVIEW.md")
    watch.AUDIT_FILE = os.path.join(watch.STATE_DIR, "alerts.jsonl")
    watch.ALERT_MODE = mode
    argv, sys.argv = sys.argv, ["watch.py"]
    test.addCleanup(setattr, sys, "argv", argv)
    return sent, work


def not_found(url):
    raise urllib.error.HTTPError(url, 404, "nf", {}, None)


WD = dict(name="Acme Space", careers="x", group="g", ats=("workday", "https://acme.wd1.myworkdayjobs.com/Ext"))


class Flow(unittest.TestCase):
    def workday(self, rounds):
        """rounds: list of job lists; self.round picks which one the fake site serves."""
        self.round = 0

        def fake(url, data=None, headers=None, method=None, as_json=True):
            if url.endswith("/wday/cxs/acme/Ext/jobs"):
                jobs = rounds[self.round] if data["offset"] == 0 else []
                return {"total": len(jobs), "jobPostings": jobs}
            if "/wday/cxs/acme/Ext/job/" in url:
                path = url.split("/wday/cxs/acme/Ext")[1]
                j = next(j for j in rounds[self.round] if j["externalPath"] == path)
                return {"jobPostingInfo": {"title": j["title"], "jobReqId": j["bulletFields"][0],
                                           "startDate": j.get("startDate", ""),
                                           "country": {"descriptor": "United States of America"},
                                           "location": j["locationsText"],
                                           "jobDescription": "<p>Design parts. Must be a U.S. citizen.</p>"}}
            not_found(url)
        return fake

    @staticmethod
    def job(req, title, path, posted="Posted 30+ Days Ago", start=""):
        return {"title": title, "externalPath": path, "locationsText": "Cedar Park, TX",
                "postedOn": posted, "bulletFields": [req], "startDate": start}

    def test_first_scan_is_silent_then_new_recent_role_alerts(self):
        today = datetime.now(timezone.utc).astimezone(triage.LOCAL_TZ).strftime("%Y-%m-%d")
        old = self.job("JR1001", "Internship - Mechanical Engineering - Spring 2027", "/job/TX/Mech_JR1001")
        new = self.job("JR1002", "Internship - Propulsion Engineering - Summer 2027", "/job/TX/Prop_JR1002",
                       "Posted Today", today)
        sent, work = run_flow(self, self.workday([[old], [old, new]]), [WD])
        watch.main()
        self.assertEqual(sent, [])                              # silent seed
        self.round = 1
        watch.main()
        self.assertEqual(len(sent), 1)
        self.assertIn("Propulsion", sent[0][0])
        self.assertIn("ID JR1002", sent[0][1])
        self.assertIn("citizenship/ITAR stated", sent[0][1])
        audit = [json.loads(l) for l in open(os.path.join(work, "state", "alerts.jsonl"))]
        self.assertEqual(audit[0]["decision"], "alert")
        self.assertEqual(audit[0]["posted_day_local"], today)
        self.assertEqual(audit[0]["country"], "US")

    def test_unseen_old_role_does_not_alert(self):
        a = self.job("JR1001", "Internship - Mechanical Engineering - Spring 2027", "/job/TX/Mech_JR1001")
        b = self.job("JR1003", "Internship - Structures - Spring 2027", "/job/TX/Struct_JR1003", start="2026-10-01")
        sent, _ = run_flow(self, self.workday([[a], [a, b]]), [WD])
        watch.main(); self.round = 1; watch.main()
        self.assertEqual(sent, [])

    def test_same_requisition_with_new_path_title_and_date_is_not_new(self):
        a = self.job("JR1001", "Internship - Mechanical Engineering - Spring 2027", "/job/TX/Mech_JR1001")
        a2 = self.job("JR1001", "Internship - Mechanical Engineering - Summer 2027", "/job/TX/Mech-Summer_JR1001-1",
                      "Posted Today", "2026-11-10")
        sent, work = run_flow(self, self.workday([[a], [a2]]), [WD], mode="newly_discovered")
        watch.main(); self.round = 1; watch.main()
        self.assertEqual(sent, [])
        ids = json.load(open(os.path.join(work, "state", "seen.json")))["Acme Space"]["ids"]
        self.assertEqual(list(ids), ["JR1001"])
        self.assertIn("/job/TX/Mech_JR1001", ids["JR1001"]["aliases"])
        self.assertIn("updated_seen", ids["JR1001"])

    def test_old_state_keyed_by_path_is_recognised_after_upgrade(self):
        a = self.job("JR1001", "Internship - Mechanical Engineering - Spring 2027", "/job/TX/Mech_JR1001")
        sent, work = run_flow(self, self.workday([[a]]), [WD], mode="newly_discovered")
        os.makedirs(os.path.join(work, "state"))
        json.dump({"Acme Space": {"seeded": True, "fp": [watch.SEARCH_VERSION, "workday", WD["ats"][1]],
                                  "ids": {"/job/TX/Mech_JR1001": {"title": "x", "first_seen": "2026-11-01 00:00 UTC"}}}},
                  open(os.path.join(work, "state", "seen.json"), "w"))
        watch.main()
        self.assertEqual(sent, [])
        ids = json.load(open(os.path.join(work, "state", "seen.json")))["Acme Space"]["ids"]
        self.assertEqual(ids["JR1001"]["first_seen"], "2026-11-01 00:00 UTC")     # first_seen preserved

    def test_greenhouse_updated_today_without_first_published_goes_to_review(self):
        self.round = 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs = [[{"id": 1, "title": "Mechanical Engineering Intern", "absolute_url": "https://x/1",
                  "location": {"name": "Hawthorne, CA"}, "updated_at": today}],
                [{"id": 1, "title": "Mechanical Engineering Intern", "absolute_url": "https://x/1",
                  "location": {"name": "Hawthorne, CA"}, "updated_at": today},
                 {"id": 2, "title": "Propulsion Engineering Intern", "absolute_url": "https://x/2",
                  "location": {"name": "Hawthorne, CA"}, "updated_at": today}]]

        def fake(url, data=None, headers=None, method=None, as_json=True):
            if url.endswith("/boards/acme/jobs"):
                return {"jobs": jobs[self.round]}
            if "/boards/acme/jobs/" in url:
                return {"title": "Propulsion Engineering Intern", "content": "Build engines.", "updated_at": today}
            not_found(url)
        gh = dict(name="Acme Rockets", careers="x", group="g", ats=("greenhouse", "acme"))
        sent, work = run_flow(self, fake, [gh], mode="today")
        watch.main(); self.round = 1; watch.main()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("To review"))
        self.assertIn("posted date unknown", sent[0][1])
        audit = [json.loads(l) for l in open(os.path.join(work, "state", "alerts.jsonl"))]
        self.assertIsNone(audit[0]["posted_day_local"])
        self.assertEqual(audit[0]["updated_raw"], today)

    def test_successfactors_error_page_uses_official_localized_link(self):
        self.round = 0
        today = datetime.now(timezone.utc).astimezone(triage.LOCAL_TZ).strftime("%b %d, %Y")
        base = "https://jobs.acme.com"
        seed = {"id": 100, "title": "Quality Intern", "urlTitle": "Quality-Intern"}
        new = {"id": 15387, "title": "Intern, Mechanical Design", "urlTitle": "Intern-Mechanical-Design"}
        good = (f'<html><link rel="canonical" href="{base}/job/Intern-Mechanical-Design/15387-en_US/">'
                f'<h1>Intern, Mechanical Design</h1><span class="jobGeoLocation">Wichita, KS, US</span>'
                f'<meta itemprop="datePosted" content="{today}"><a>Apply now</a></html>')

        def fake(url, data=None, headers=None, method=None, as_json=True):
            if url.endswith("/services/recruiting/v1/jobs"):
                items = [seed] if self.round == 0 else [seed, new]
                return {"jobSearchResult": [{"response": j} for j in items] if data["pageNumber"] == 0 else []}
            if url == f"{base}/job/Intern-Mechanical-Design/15387/":
                return "<html><title>Error</title>An error has occurred.</html>", url   # HTTP 200 error page
            if url == f"{base}/job/Intern-Mechanical-Design/15387-en_US/":
                return good, url
            not_found(url)
        sf = dict(name="Acme Jets", careers="x", group="g", ats=("successfactors", base))
        sent, _ = run_flow(self, fake, [sf], mode="today")
        watch.main(); self.round = 1; watch.main()
        self.assertEqual(len(sent), 1)
        self.assertIn("Mechanical Design", sent[0][0])
        self.assertEqual(sent[0][2], f"{base}/job/Intern-Mechanical-Design/15387-en_US/")
        self.assertIn("Wichita, KS, US [US]", sent[0][1])


if __name__ == "__main__":
    unittest.main()
