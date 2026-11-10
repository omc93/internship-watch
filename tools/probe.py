#!/usr/bin/env python3
"""Diagnostics: for companies that aren't working, look for their job system.

Prints compact leads: job-system links found on careers pages, valid
Greenhouse/Lever/Ashby boards for name-based slugs, and Workday tenants that
respond. Run via the "Probe" workflow; read the log.
"""
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watch  # noqa: E402
from companies import COMPANIES  # noqa: E402

ATS_HOST = re.compile(
    r"https?://[a-z0-9.-]*(myworkdayjobs|myworkdaysite|greenhouse|lever\.co|ashbyhq|smartrecruiters|"
    r"icims|taleo|oraclecloud|successfactors|sapsf|avature|eightfold|phenom|talentbrew|jobvite|"
    r"workable|paylocity|ultipro|ukg|adp\.com|dayforce|brassring|jibe|clinch|radancy|"
    r"recruitee|bamboohr|breezy|rippling|teamtailor|personio|jobs2web|rmkcdn)[a-z0-9./?=&_%-]*",
    re.I)


def get(url):
    try:
        page, final = watch.http(url, as_json=False)
        return page, final, 200
    except Exception as e:
        return "", url, getattr(e, "code", type(e).__name__)


def page_leads(url):
    page, final, code = get(url)
    hosts = sorted({m.group(0)[:110] for m in ATS_HOST.finditer(page)})
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return {"url": url, "code": code, "final": final if final != url else "",
            "title": watch.clean(title.group(1))[:60] if title else "",
            "leads": hosts[:12], "phenom": "phApp.ddo" in page,
            "job_links": len(re.findall(r'href="[^"]*/job[s]?/', page))}


def slugs(c):
    name = re.sub(r"\(.*?\)", "", c["name"]).lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", name)
    host = urllib.parse.urlparse(c["careers"]).netloc.lower()
    dom = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "com", "co", "us", "aero",
                                                 "tech", "ai", "space", "energy", "io", "group")]
    out = {"".join(words), "-".join(words), words[0], "".join(words[:2])}
    out.update(dom)
    return sorted(x for x in out if len(x) > 2)


def board_hits(c):
    hits = []
    for s in slugs(c):
        for kind, url in (("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{s}"),
                          ("lever", f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1"),
                          ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{s}")):
            try:
                d = watch.http(url)
                label = d.get("name", "") if isinstance(d, dict) else ""
                n = len(d.get("jobs", [])) if isinstance(d, dict) else len(d)
                hits.append(f"{kind}:{s} name={label!r} n={n}")
            except Exception:
                pass
    return hits


def workday_hits(c):
    tenants = set(slugs(c))
    hits = []
    for t in sorted(tenants):
        for wd in ("wd1", "wd3", "wd5", "wd12", "wd103", "wd501"):
            url = f"https://{t}.{wd}.myworkdayjobs.com/"
            try:
                page, final = watch.http(url, as_json=False)
                hits.append(f"{url} -> {final}")
            except Exception as e:
                code = getattr(e, "code", None)
                if code and code != 404:
                    hits.append(f"{url} -> HTTP {code}")
    return hits


def probe(c, extra):
    out = {"name": c["name"], "pages": [page_leads(u) for u in [c["careers"]] + extra.get(c["name"], [])],
           "boards": board_hits(c), "workday": []}
    return out


def main():
    names = [n.strip().lower() for n in os.environ.get("PROBE", "").split(",") if n.strip()]
    extra = json.loads(os.environ.get("PROBE_URLS", "{}") or "{}")
    if names == ["failing"]:
        res = json.load(open(watch.RESOLVED_FILE))
        names = [n.lower() for n, v in res.items() if v.get("status") not in ("ok", "skipped")]
    cs = [c for c in COMPANIES if c["name"].lower() in names or any(n in c["name"].lower() for n in names)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        for r in pool.map(lambda c: probe(c, extra), cs):
            print("=" * 8, r["name"])
            for p in r["pages"]:
                print(f"  page {p['code']} {p['url']} {('-> ' + p['final']) if p['final'] else ''} "
                      f"title={p['title']!r} phenom={p['phenom']} joblinks={p['job_links']}")
                for l in p["leads"]:
                    print("     lead", l)
            for b in r["boards"]:
                print("  board", b)
            for w in r["workday"]:
                print("  workday", w)
    def wd(tenant):
        import urllib.request
        lines = []
        for n in ("wd1", "wd3", "wd5", "wd12", "wd103", "wd108", "wd501"):
            url = f"https://{tenant}.{n}.myworkdayjobs.com/"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": watch.UA, "Accept": "text/html"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read(200000).decode("utf-8", "replace")
                    sites = sorted(set(re.findall(r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]{3,60})", body)))
                    lines.append(f"WD {tenant}.{n} 200 final={r.geturl()} sites={sites[:6]}")
            except Exception as e:
                code = getattr(e, "code", type(e).__name__)
                if code not in (404, "URLError", 500):
                    lines.append(f"WD {tenant}.{n} {code}")
        return lines
    tenants = json.loads(os.environ.get("WD_TENANTS", "[]") or "[]")
    with ThreadPoolExecutor(max_workers=12) as pool:
        for lines in pool.map(wd, tenants):
            for l in lines:
                print(l)
    specs = json.loads(os.environ.get("ATS_TESTS", "[]") or "[]")

    def test(spec):
        try:
            jobs = watch.run_ats(tuple(spec))
            interns = [p for p in jobs if watch.is_internship(p)]
            return f"OK {spec} total={len(jobs)} interns={len(interns)} e.g. " + \
                "; ".join(f"{p['title'][:50]} [{p['location'][:25]}]" for p in interns[:3])
        except Exception as e:
            return f"FAIL {spec} {type(e).__name__}: {str(e)[:120]}"
    with ThreadPoolExecutor(max_workers=12) as pool:
        for line in pool.map(test, specs):
            print("ATS", line)
    for url in json.loads(os.environ.get("RAW_URLS", "[]") or "[]"):
        p = page_leads(url)
        print("=" * 8, "RAW", url, p["code"], p["final"], p["title"], "phenom", p["phenom"],
              "joblinks", p["job_links"])
        for l in p["leads"]:
            print("     lead", l)


if __name__ == "__main__":
    main()
