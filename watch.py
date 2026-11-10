#!/usr/bin/env python3
"""Internship watcher.

Checks each company's own job system for internship postings, compares them
with what it has already seen, and sends new ones to your phone via ntfy.

Usage:
  python watch.py              normal run (sends alerts for new postings)
  python watch.py --test       send one test notification and exit
  python watch.py --only NAME  check only companies whose name contains NAME

Environment:
  NTFY_TOPIC   your private ntfy topic name (required for alerts)
  NTFY_SERVER  defaults to https://ntfy.sh
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from companies import COMPANIES

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
RESOLVED_FILE = os.path.join(STATE_DIR, "resolved.json")
STATUS_FILE = os.path.join(ROOT, "STATUS.md")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 25
MAX_ALERTS_PER_RUN = 20
REDISCOVER_HOURS = 24
# Bump when search logic changes, so companies re-record silently instead of
# alerting on postings that were there all along.
SEARCH_VERSION = 2

INTERN_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op|coop)\b", re.I)
EXCLUDE_RE = re.compile(r"\b(high school|internal|international|skillbridge)\b|"
                        r"\([mfwdx]/[mfwdx]/[mfwdx]\)|praktik|werkstudent|stagiaire", re.I)
# Locations clearly outside the US. Postings with no location, or a US/remote
# location, are kept.
NON_US = re.compile(
    r"\b(india|china|germany|deutschland|france|uk|united kingdom|england|"
    r"scotland|ireland|mexico|méxico|brazil|brasil|canada|ontario|quebec|"
    r"québec|british columbia|alberta|japan|korea|singapore|malaysia|"
    r"philippines|vietnam|thailand|indonesia|taiwan|australia|new zealand|"
    r"poland|czech|hungary|romania|slovakia|italy|italia|spain|españa|"
    r"portugal|netherlands|belgium|switzerland|austria|sweden|norway|"
    r"denmark|finland|israel|turkey|türkiye|saudi|uae|dubai|qatar|egypt|"
    r"morocco|south africa|argentina|chile|colombia|peru|costa rica|"
    r"bangalore|bengaluru|hyderabad|pune|chennai|shanghai|suzhou|beijing|"
    r"shenzhen|munich|münchen|berlin|stuttgart|toulouse|paris|hamburg|"
    r"bremen|madrid|getafe|montreal|montréal|toronto|mississauga|"
    r"querétaro|queretaro|monterrey|chihuahua|juarez|juárez|tijuana|"
    r"guadalajara|krakow|kraków|wroclaw|wrocław|rzeszow|rzeszów|"
    r"budapest|bucharest|bratislava|prague|brno|eindhoven|veldhoven)\b",
    re.I)

# Only engineering internships trigger alerts. A title must match ENG_RE and
# must not match NONENG_RE. Edit these lists to widen or narrow alerts.
ENG_RE = re.compile(
    r"engineer|engineering|\beng\b|mechanical|aerospace|aeronautic|astronaut|propulsion|structur|"
    r"manufactur|thermal|fluid|material|composite|additive|weld|machin|tooling|stress|dynamics|"
    r"aerodynamic|avionic|\bgnc\b|guidance|controls?\b|mechatronic|robotic|hardware|electrical|"
    r"electronics|\brf\b|systems|integration|\btest|validation|reliability|quality|industrial|"
    r"process|product development|r&d|research and development|design|\bcad\b|powertrain|battery|"
    r"vehicle|chassis|engine|spacecraft|satellite|rocket|launch|aircraft|flight|ground support|\bgse\b|"
    r"mission|payload|analysis|technical|maintenance|facilities|mining|hydraulic|calibration|"
    r"continuous improvement|lean|automation|\bm&p\b|metallurg|nuclear|energy|power",
    re.I)
NONENG_RE = re.compile(
    r"software|\bit\b|information technology|information systems|\bdata\b|analytics|cyber|security|"
    r"machine learning|\bml\b|\bai\b|artificial intelligence|computer science|developer|devops|web|"
    r"cloud|finance|financial|accounting|\bhr\b|human resource|people|talent|recruit|marketing|sales|"
    r"supply chain|procurement|purchasing|sourcing|logistics|legal|law|communications|public relations|"
    r"business|strategy|brand|interior design|graphic|\bux\b|\bui\b|user experience|product manage|"
    r"program manage|project manage|government relations|policy|customer experience|events|"
    r"executive|administrative|office|audit|tax|treasury|investor|real estate|ehs|health|safety|"
    r"environmental, health|mba|phd|doctor|firmware|embedded",
    re.I)
def is_engineering(t):
    return bool(ENG_RE.search(t)) and not NONENG_RE.search(t)


# ---------------------------------------------------------------- HTTP

def http(url, data=None, headers=None, method=None, as_json=True):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode() if not isinstance(data, bytes) else data
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                text = raw.decode(r.headers.get_content_charset() or "utf-8", "replace")
                if as_json:
                    return json.loads(text)
                return text, r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410, 422):
                raise
            last = e
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2)
    raise last


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def posting(pid, title, url, location="", posted=""):
    return {"id": str(pid), "title": clean(title), "url": url,
            "location": clean(location), "posted": clean(str(posted or ""))}


# ---------------------------------------------------------------- job systems

def fetch_workday(url):
    p = urllib.parse.urlparse(url)
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
    if "myworkdaysite.com" in p.netloc:          # wdN.myworkdaysite.com/recruiting/TENANT/SITE
        tenant, site = segs[1], segs[2]
        api = f"https://{p.netloc}/wday/cxs/{tenant}/{site}/jobs"
        link_base = f"https://{p.netloc}/recruiting/{tenant}/{site}"
    else:
        tenant, site = p.netloc.split(".")[0], segs[0]
        api = f"https://{p.netloc}/wday/cxs/{tenant}/{site}/jobs"
        link_base = f"https://{p.netloc}/{site}"
    out = {}
    for text in ("intern", "internship", "co-op"):
        offset, total = 0, None
        while offset < 400:
            d = http(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": text})
            if total is None:
                total = d.get("total", 0)
            jobs = d.get("jobPostings", [])
            for j in jobs:
                path = j.get("externalPath", "")
                out[path] = posting(path, j.get("title", ""), f"{link_base}{path}",
                                    j.get("locationsText", ""), j.get("postedOn", ""))
            offset += 20
            if not jobs or offset >= total:
                break
    return list(out.values())


def fetch_greenhouse(slug):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [posting(j["id"], j["title"], j["absolute_url"],
                    (j.get("location") or {}).get("name", ""),
                    j.get("first_published") or j.get("updated_at", ""))
            for j in d.get("jobs", [])]


def fetch_lever(slug):
    d = http(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [posting(j["id"], j["text"], j["hostedUrl"],
                    (j.get("categories") or {}).get("location", ""), j.get("createdAt", ""))
            for j in d]


def fetch_ashby(slug):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [posting(j["id"], j["title"], j.get("jobUrl", ""), j.get("location", ""),
                    j.get("publishedAt", ""))
            for j in d.get("jobs", [])]


def fetch_smartrecruiters(cid):
    out = []
    for q in ("intern", "internship", "co-op"):
        d = http(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?"
                 f"q={urllib.parse.quote(q)}&limit=100&country=us")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append(posting(j["id"], j["name"],
                               f"https://jobs.smartrecruiters.com/{cid}/{j['id']}",
                               ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                     loc.get("country")) if x),
                               j.get("releasedDate", "")))
    return out


def fetch_oracle(url):
    p = urllib.parse.urlparse(url)
    site = re.search(r"/sites/([^/?#]+)", p.path).group(1)
    out = []
    for kw in ("intern", "internship", "co-op"):
        q = (f"https://{p.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?"
             f"onlyData=true&expand=requisitionList.secondaryLocations&"
             f"finder=findReqs;siteNumber={site},keyword={urllib.parse.quote(kw)},"
             f"limit=200,sortBy=POSTING_DATES_DESC")
        d = http(q)
        for item in d.get("items", []):
            for j in item.get("requisitionList", []):
                out.append(posting(j["Id"], j.get("Title", ""),
                                   f"https://{p.netloc}/hcmUI/CandidateExperience/en/sites/{site}/job/{j['Id']}",
                                   j.get("PrimaryLocation", ""), j.get("PostedDate", "")))
    return out


def _phenom_jobs(page):
    i = page.find('"eagerLoadRefineSearch"')
    if i < 0:
        return None
    j = page.find("{", i)
    obj, _ = json.JSONDecoder().raw_decode(page[j:])
    return (obj.get("data") or {}).get("jobs", [])


def fetch_phenom(base):
    base = base.rstrip("/")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        start, total = 0, None
        while start < 600:
            page, _ = http(f"{base}/search-results?keywords={kw}&from={start}&s=1", as_json=False)
            i = page.find('"eagerLoadRefineSearch"')
            if i < 0:
                if start == 0 and kw == "intern":
                    raise ValueError("no Phenom job data on page")
                break
            obj, _ = json.JSONDecoder().raw_decode(page[page.find("{", i):])
            if total is None:
                total = obj.get("totalHits") or obj.get("hits") or 0
            jobs = (obj.get("data") or {}).get("jobs", [])
            for j in jobs:
                jid = j.get("jobId") or j.get("reqId") or j.get("jobSeqNo")
                loc = j.get("cityStateCountry") or j.get("location") or ""
                out[jid] = posting(jid, j.get("title", ""), f"{base}/job/{jid}", loc,
                                   j.get("postedDate", ""))
            start += len(jobs)
            if not jobs or start >= total:
                break
    return list(out.values())

LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def links_from(page, base_url, pattern):
    out = {}
    pat = re.compile(pattern)
    for href, text in LINK_RE.findall(page):
        href = html.unescape(href)
        if not pat.search(href):
            continue
        full = urllib.parse.urljoin(base_url, href)
        title = clean(text)
        if not title or len(title) > 200:
            continue
        out[full] = posting(full, title, full)
    return list(out.values())


def fetch_radancy(base):
    """Radancy / TalentBrew career sites (careers.example.com/search-jobs/KEYWORD)."""
    base = base.rstrip("/")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        for pg in (1, 2, 3, 4):
            try:
                page, final = http(f"{base}/search-jobs/{kw}?p={pg}", as_json=False)
            except Exception:
                break
            found = links_from(page, final, r"/job/")
            for p in found:
                out[p["id"]] = p
            if len(found) < 10:
                break
    if not out:
        raise ValueError("no job links found")
    return list(out.values())


def fetch_html(url, pattern=r"/job"):
    page, final = http(url, as_json=False)
    found = links_from(page, final, pattern)
    if not found:
        raise ValueError("no job links found")
    return found


def fetch_amazon(query, must="robotics"):
    url = ("https://www.amazon.jobs/en/search.json?result_limit=100&sort=recent"
           f"&normalized_country_code%5B%5D=USA&base_query={urllib.parse.quote(query)}")
    d = http(url)
    out = []
    for j in d.get("jobs", []):
        if must.lower() not in json.dumps(j).lower():
            continue
        out.append(posting(j.get("id_icims") or j.get("id"), j.get("title", ""),
                           "https://www.amazon.jobs" + j.get("job_path", ""),
                           j.get("normalized_location") or j.get("location", ""),
                           j.get("posted_date", "")))
    return out


def fetch_successfactors(base):
    """SAP SuccessFactors career sites (jobs.example.com/job/Title/12345/)."""
    base = base.rstrip("/")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        # Newer "Career Site Builder" sites load results from this JSON service.
        try:
            for page_no in range(0, 5):
                d = http(f"{base}/services/recruiting/v1/jobs",
                         {"locale": "en_US", "pageNumber": page_no, "sortBy": "date",
                          "keywords": kw, "location": "", "facetFilters": {}, "brand": "",
                          "skills": [], "categoryId": 0, "alertId": "", "rcmCandidateId": ""})
                items = d.get("jobSearchResult", [])
                for it in items:
                    j = it.get("response", it)
                    jid = j.get("id")
                    loc = j.get("jobLocationShort") or j.get("jobLocationState") or ""
                    if isinstance(loc, list):
                        loc = "; ".join(map(str, loc))
                    out[str(jid)] = posting(jid, j.get("unifiedStandardTitle") or j.get("title", ""),
                                            f"{base}/job/{j.get('urlTitle', 'job')}/{jid}/", loc,
                                            j.get("unifiedStandardStart", ""))
                if len(items) < 10:
                    break
        except Exception:
            pass
        if out:
            continue
        # Classic sites render results as HTML.
        for start in (0, 25, 50, 75):
            page, final = http(f"{base}/search/?q={kw}&sortColumn=referencedate&sortDirection=desc"
                               f"&startrow={start}", as_json=False)
            found = links_from(page, final, r"/job/[^/]+/\d+(?:-[a-z]{2}_[A-Z]{2})?/?$")
            for p in found:
                m = re.search(r"/(\d+)(?:-[a-z]{2}_[A-Z]{2})?/?$", p["url"])
                if m:
                    p["id"] = m.group(1)
                out[p["id"]] = p
            if len(found) < 20:
                break
    if not out:
        raise ValueError("no SuccessFactors results")
    return list(out.values())


def fetch_jibe(base):
    """Jibe-powered career sites (careers.example.com/api/jobs)."""
    base = base.rstrip("/")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        for page_no in range(1, 30):
            d = http(f"{base}/api/jobs?keywords={urllib.parse.quote(kw)}&page={page_no}&limit=100")
            items = d.get("jobs", [])
            for item in items:
                j = item.get("data", item)
                jid = j.get("req_id") or j.get("slug")
                loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
                out[str(jid)] = posting(jid, j.get("title", ""), f"{base}/jobs/{j.get('slug', jid)}",
                                        loc, j.get("posted_date", ""))
            total = d.get("totalCount") or d.get("count") or 0
            if not items or len(out) >= total or len(items) < 10:
                break
    return list(out.values())

def fetch_eightfold(host, domain):
    host = host.rstrip("/")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        q = urllib.parse.quote(kw)
        try:
            d = http(f"{host}/api/apply/v2/jobs?domain={domain}&query={q}&start=0&num=100&sort_by=timestamp")
            positions = d.get("positions", [])
        except urllib.error.HTTPError:
            positions, start = [], 0
            while start < 300:
                d = http(f"{host}/api/pcsx/search?domain={domain}&query={q}&location=&start={start}"
                         f"&sort_by=timestamp", headers={"Referer": f"{host}/careers"})
                data = d.get("data") or d
                batch = data.get("positions", [])
                positions += batch
                start += len(batch)
                if not batch or start >= (data.get("count") or 0):
                    break
        for j in positions:
            loc = j.get("location") or "; ".join(j.get("locations") or [])
            url = j.get("canonicalPositionUrl") or j.get("positionUrl") or f"/careers/job/{j['id']}"
            out[str(j["id"])] = posting(j["id"], j.get("name", ""), urllib.parse.urljoin(host + "/", url),
                                        loc, j.get("t_create") or j.get("postedTs") or "")
    return list(out.values())


def fetch_taleo(host, section):
    """Oracle Taleo career sections (host.taleo.net/careersection/SECTION)."""
    host = host.rstrip("/")
    page, _ = http(f"{host}/careersection/{section}/jobsearch.ftl?lang=en", as_json=False)
    m = re.search(r"portal[=:\"' ]+(\d{6,})", page)
    if not m:
        raise ValueError("Taleo portal number not found")
    out = {}
    for kw in ("intern", "co-op"):
        for page_no in (1, 2, 3):
            body = {"multilineEnabled": False,
                    "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                    "fieldData": {"fields": {"KEYWORD": kw, "LOCATION": ""}, "valid": True},
                    "filterSelectionParam": {"searchFilterSelections": []},
                    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                    "pageNo": page_no}
            d = http(f"{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={m.group(1)}", body,
                     headers={"tz": "GMT-06:00", "tzname": "America/Chicago"})
            reqs = d.get("requisitionList", [])
            for r in reqs:
                cols = r.get("column", [])
                title = cols[0] if cols else ""
                loc = cols[1] if len(cols) > 1 else ""
                if isinstance(loc, str) and loc.startswith("["):
                    try:
                        loc = "; ".join(json.loads(loc))
                    except Exception:
                        pass
                cid = r.get("contestNo") or r.get("jobId")
                out[str(cid)] = posting(cid, title,
                                        f"{host}/careersection/{section}/jobdetail.ftl?job={cid}&lang=en",
                                        loc, cols[2] if len(cols) > 2 else "")
            if len(reqs) < 25:
                break
    return list(out.values())


def fetch_sfxml(host, company):
    """SuccessFactors XML job feed (careerN.successfactors.com/career?company=X&resultType=XML)."""
    host = host.rstrip("/")
    page, _ = http(f"{host}/career?company={company}&career_ns=job_listing_summary&resultType=XML",
                   as_json=False)
    out = []
    for block in re.findall(r"<Job>(.*?)</Job>", page, re.S):
        f = {k.lower(): re.sub(r"^<!\[CDATA\[|\]\]>$", "", v.strip())
             for k, v in re.findall(r"<([\w-]+)>(.*?)</\1>", block, re.S)}
        title = f.get("jobtitle") or f.get("title", "")
        jid = next((v for k, v in f.items() if "reqid" in k.replace("-", "")), "") or title
        loc = next((v for k, v in f.items() if "location" in k and len(v) < 120), "")
        date = next((v for k, v in f.items() if "date" in k and len(v) < 40), "")
        out.append(posting(jid, title,
                           f"{host}/career?company={company}&career_job_req_id={jid}&career_ns=job_listing",
                           loc, date))
    if not out:
        raise ValueError("no jobs in XML feed")
    return out


def fetch_cws(base):
    """Career sites built on the "CWS" WordPress plugin (/job-search-results/, /job/ID/slug/)."""
    base = base.rstrip("/")
    page, _ = http(f"{base}/job-search-results/", as_json=False)
    m = re.search(r"companies(?:/|%2F|\\/)([0-9a-f-]{36})", page)
    if not m:
        near = [page[max(0, i.start() - 80):i.start() + 160] for i in re.finditer(r"m-cloud\.io", page)][:2]
        raise ValueError(f"CWS company id not found; near={near}")
    out = {}
    for kw in ("intern", "internship", "co-op"):
        for offset in (0, 100, 200):
            text, _ = http("https://jobsapi-google.m-cloud.io/api/job/search?pageSize=100"
                           f"&offset={offset}&companyName=companies%2F{m.group(1)}&query={kw}"
                           "&orderBy=posting_publish_time%20desc", as_json=False,
                           headers={"Referer": base + "/", "Origin": base})
            text = text.strip()
            if not text.startswith("{"):
                text = text[text.find("{"):text.rfind("}") + 1]
            d = json.loads(text)
            items = d.get("searchResults") or d.get("queryResult") or []
            if not items and offset == 0 and kw == "intern" and "searchResults" not in d:
                raise ValueError(f"unexpected CWS response keys: {list(d)[:8]}")
            for item in items:
                j = item.get("job", item)
                loc = ", ".join(x for x in (j.get("primary_city"), j.get("primary_state"),
                                            j.get("primary_country")) if x)
                slug = re.sub(r"[^a-z0-9]+", "-", j.get("title", "").lower()).strip("-")
                out[str(j["id"])] = posting(j["id"], j.get("title", ""), f"{base}/job/{j['id']}/{slug}/",
                                            loc, j.get("open_date", ""))
            if len(items) < 100:
                break
    return list(out.values())

FETCHERS = {
    "workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
    "ashby": fetch_ashby, "smartrecruiters": fetch_smartrecruiters, "oracle": fetch_oracle,
    "phenom": fetch_phenom, "radancy": fetch_radancy, "html": fetch_html,
    "amazon": fetch_amazon, "successfactors": fetch_successfactors, "jibe": fetch_jibe,
    "eightfold": fetch_eightfold, "taleo": fetch_taleo, "sfxml": fetch_sfxml,
    "cws": fetch_cws,
}


def run_ats(ats):
    kind, *args = ats
    return FETCHERS[kind](*args)


# ---------------------------------------------------------------- discovery

PATTERNS = [
    ("workday", re.compile(r"https?://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
                           r"(?!wday\b)([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?"
                              r"([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("oracle", re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
                          r"[a-zA-Z-]+/sites/([A-Za-z0-9_]+)")),
]
SKIP_SLUGS = {"embed", "js", "v1", "jobs", "job", "api", "careers", "search"}


def candidates_from(page):
    found = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(page):
            if kind == "workday":
                found.append(("workday", f"https://{m.group(1)}/{m.group(2)}"))
            elif kind == "oracle":
                found.append(("oracle", f"https://{m.group(1)}/hcmUI/CandidateExperience/en/sites/{m.group(2)}"))
            elif m.group(1).lower() not in SKIP_SLUGS:
                found.append((kind, urllib.parse.unquote(m.group(1))))
    if "phenompeople" in page or "phApp.ddo" in page:
        found.append(("phenom?", ""))
    low = page.lower()
    if "talentbrew" in low:
        found.append(("radancy?", ""))
    if "rmkcdn.successfactors" in low or "jobs2web" in low or "sapsf.com" in low or "sapsf.eu" in low \
            or "successfactors.eu" in low or "career5.successfactors" in low:
        found.append(("successfactors?", ""))
    if "jibecdn" in low:
        found.append(("jibe?", ""))
    for m in re.finditer(r"https?://([a-z0-9-]+\.taleo\.net)/careersection/([a-z0-9_]+)/", page, re.I):
        found.append(("taleo", f"https://{m.group(1)}|{m.group(2)}"))
    for m in re.finditer(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9_-]+)/([A-Za-z0-9_-]+)", page):
        found.append(("workday", f"https://{m.group(1)}.myworkdaysite.com/recruiting/{m.group(2)}/{m.group(3)}"))
    seen, out = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def slug_guesses(name, careers=""):
    base = re.sub(r"\(.*?\)", "", name).lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    g = ["".join(words), "-".join(words)]
    host = urllib.parse.urlparse(careers).netloc.lower().split(".")
    host = [h for h in host if h not in ("www", "careers", "jobs", "com", "co", "io", "ai", "aero",
                                         "space", "tech", "xyz", "us", "energy", "bot")]
    if host and len(host[0]) > 3:
        g.append(host[0])
    return list(dict.fromkeys(x for x in g if x))


def try_candidate(c, careers_url):
    kind, arg = c
    if kind == "phenom?":
        m = re.match(r"(https?://[^/]+/[a-z]+/[a-z]{2})(?=/|$|\?)", careers_url)
        base = m.group(1) if m else careers_url.rstrip("/")
        c = ("phenom", base)
    elif kind in ("radancy?", "successfactors?", "jibe?"):
        p = urllib.parse.urlparse(careers_url)
        c = (kind[:-1], f"{p.scheme}://{p.netloc}")
    elif kind == "taleo":
        host, section = arg.split("|")
        c = ("taleo", host, section)
    jobs = run_ats(c)
    return c, jobs


def discover(company):
    """Return (ats, jobs, note) or raise."""
    name, url = company["name"], company["careers"]
    tried = []
    pages = []
    try:
        page, final = http(url, as_json=False)
        pages.append((page, final))
        # one hop: follow links that look like job search pages
        hops = []
        for href, text in LINK_RE.findall(page):
            t = (href + " " + clean(text)).lower()
            if any(k in t for k in ("search-jobs", "job-search", "jobs", "openings",
                                    "positions", "opportunities", "apply", "join")):
                full = urllib.parse.urljoin(final, html.unescape(href))
                if full.startswith("http") and full not in hops and full != final:
                    hops.append(full)
        for h in hops[:6]:
            try:
                pages.append(http(h, as_json=False))
            except Exception:
                pass
    except Exception as e:
        tried.append(f"careers page: {type(e).__name__}")

    cands = []
    for page, final in pages:
        for c in candidates_from(page):
            if c[0].endswith("?"):
                c = (c[0], final)
            if c not in cands:
                cands.append(c)
    for c in cands:
        try:
            ats, jobs = try_candidate(c, c[1] if c[0].endswith("?") else url)
            if jobs or ats[0] in ("greenhouse", "lever", "ashby", "workday", "oracle", "taleo"):
                return ats, jobs, "found on careers page"
        except Exception as e:
            tried.append(f"{c[0]}:{c[1]} {type(e).__name__}")

    # Startups mostly use Greenhouse / Lever / Ashby with a slug like their name.
    for slug in slug_guesses(name, url):
        for kind in ("greenhouse", "ashby", "lever"):
            try:
                jobs = run_ats((kind, slug))
                if jobs:
                    return (kind, slug), jobs, "guessed from company name - verify"
            except Exception:
                pass
    raise LookupError("; ".join(tried[:4]) or "no job system found on careers page")


# ---------------------------------------------------------------- main logic

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


MAX_AGE_DAYS = 4   # postings older than this are recorded silently, never alerted
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def age_days(posted):
    """Best-effort age of a posting in days, or None if unknown."""
    t = str(posted or "").strip().lower()
    if not t:
        return None
    now = datetime.now(timezone.utc)
    if re.fullmatch(r"\d{12,14}", t):                      # epoch milliseconds
        return (now - datetime.fromtimestamp(int(t) / 1000, timezone.utc)).days
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)            # ISO date
    if m:
        d = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        return (now - d).days
    if "today" in t or "just posted" in t or "hour" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", t)                   # "Posted 5 Days Ago", "30+ Days"
    if m:
        return int(m[1])
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", t)   # "Nov 3, 2026"
    if m and m[1] in MONTHS:
        return (now - datetime(int(m[3]), MONTHS[m[1]], int(m[2]), tzinfo=timezone.utc)).days
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})", t)   # "3 Nov 2026"
    if m and m[2] in MONTHS:
        return (now - datetime(int(m[3]), MONTHS[m[2]], int(m[1]), tzinfo=timezone.utc)).days
    return None


def is_internship(p):
    t = p["title"]
    if not INTERN_RE.search(t) or EXCLUDE_RE.search(t):
        return False
    if p["location"] and NON_US.search(p["location"]) and not re.search(
            r"\b(united states|usa|us|remote)\b", p["location"], re.I):
        return False
    if NON_US.search(t):
        return False
    return True


def check(company, resolved):
    """Returns dict(status, ats, jobs, note)."""
    name = company["name"]
    ats = company.get("ats")
    if ats and ats[0] == "none":
        return {"status": "skipped", "note": ats[1], "jobs": []}
    errors = []
    for source, cand in (("config", ats), ("cache", resolved.get(name, {}).get("ats"))):
        if not cand:
            continue
        cand = tuple(cand)
        try:
            return {"status": "ok", "ats": list(cand), "jobs": run_ats(cand), "note": source}
        except Exception as e:
            errors.append(f"{cand[0]} failed ({type(e).__name__}: {str(e)[:80]})")
    last = resolved.get(name, {}).get("last_discovery", 0)
    if not errors and time.time() - last < REDISCOVER_HOURS * 3600:
        return {"status": "unresolved", "note": resolved[name].get("note", ""), "jobs": []}
    try:
        found, jobs, note = discover(company)
        return {"status": "ok", "ats": list(found), "jobs": jobs, "note": note,
                "discovered": True}
    except Exception as e:
        errors.append(str(e)[:200])
        return {"status": "error", "note": " | ".join(errors), "jobs": [],
                "discovery_failed": True}


def notify(title, message, click=None, tags="briefcase", priority="default"):
    topic = (os.environ.get("NTFY_TOPIC") or "").strip().strip("/")
    if not topic:
        print(f"[no NTFY_TOPIC] {title}: {message}")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = topic.rsplit("/", 1)[-1]   # accept "ntfy.sh/name" or a full URL too
    headers = {"Title": title.encode("utf-8").decode("latin-1", "replace"),
               "Tags": tags, "Priority": priority, "Content-Type": "text/plain; charset=utf-8"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception as e:
        print(f"notify failed: {e}")


def main():
    args = sys.argv[1:]
    if "--test" in args:
        notify("Internship watch is connected",
               "Test alert. New internship postings will show up here.", tags="white_check_mark")
        return
    only = args[args.index("--only") + 1].lower() if "--only" in args else None

    os.makedirs(STATE_DIR, exist_ok=True)
    seen = load(SEEN_FILE, {})
    resolved = load(RESOLVED_FILE, {})
    companies = [c for c in COMPANIES if not only or only in c["name"].lower()]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda c: (c, check(c, resolved)), companies))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    alerts, rows = [], []
    for company, r in results:
        name = company["name"]
        entry = resolved.setdefault(name, {})
        if r.get("ats"):
            entry["ats"] = r["ats"]
        if r.get("discovered") or r.get("discovery_failed"):
            entry["last_discovery"] = time.time()
        entry["note"] = r.get("note", "")
        entry["status"] = r["status"]
        entry["fails"] = 0 if r["status"] == "ok" else entry.get("fails", 0) + 1

        interns = list({p["id"]: p for p in r["jobs"] if is_internship(p)}.values())
        state = seen.setdefault(name, {"seeded": False, "ids": {}})
        if r["status"] == "ok":
            fp = [SEARCH_VERSION] + list(r.get("ats") or [])
            if state.get("fp") != fp:
                state["fp"] = fp     # new job system or search logic: record silently
                state["seeded"] = False
            new = [p for p in interns if p["id"] not in state["ids"]]
            for p in interns:
                state["ids"].setdefault(p["id"], {"title": p["title"], "first_seen": now})
            if state["seeded"]:
                for p in new:
                    age = age_days(p["posted"])
                    if is_engineering(p["title"]) and (age is None or age <= MAX_AGE_DAYS):
                        alerts.append((name, p))
            else:
                state["seeded"] = True   # first successful check: record silently
        rows.append((company["group"], name, r["status"],
                     (r.get("ats") or [""])[0], len(r["jobs"]),
                     sum(1 for p in interns if is_engineering(p["title"])), r.get("note", "")))

    for name, p in alerts[:MAX_ALERTS_PER_RUN]:
        bits = [x for x in (p["location"], p["posted"]) if x]
        notify(f"{name}: {p['title']}", " · ".join(bits) or "New internship posting",
               click=p["url"])
        print(f"NEW  {name}: {p['title']}  {p['url']}")
    if len(alerts) > MAX_ALERTS_PER_RUN:
        rest = alerts[MAX_ALERTS_PER_RUN:]
        names = sorted({n for n, _ in rest})
        notify(f"+{len(rest)} more new internships",
               "Also new at: " + ", ".join(names) + ". See STATUS.md in your repo.",
               tags="briefcase")

    # Warn once when a company that used to work has failed for 12 checks in a row.
    for company, r in results:
        e = resolved[company["name"]]
        if e["fails"] == 12 and seen.get(company["name"], {}).get("seeded"):
            notify(f"Check broken: {company['name']}",
                   "This company's job site has failed for about 12 hours. "
                   "Ask Claude to look at it.", tags="warning")

    if alerts:   # keep a running list of recent alerts, newest first
        log = os.path.join(ROOT, "ALERTS.md")
        try:
            prev = open(log).read().split("\n", 2)[2] if os.path.exists(log) else ""
        except IndexError:
            prev = ""
        lines = [f"- {now} · **{n}** · [{p['title']}]({p['url']}) · {p['location']}" for n, p in alerts]
        body = "\n".join(lines + prev.splitlines()[:300])
        with open(log, "w") as f:
            f.write("# Recent internship alerts\n\n" + body + "\n")
    save(SEEN_FILE, seen)
    save(RESOLVED_FILE, resolved)
    write_status(rows, alerts, now)
    ok = sum(1 for r in rows if r[2] == "ok")
    print(f"\n{ok}/{len(rows)} companies checked OK, {len(alerts)} new internship(s).")


def write_status(rows, alerts, now):
    lines = ["# Internship watch status\n",
             f"Working: {sum(1 for r in rows if r[2] == 'ok')} of {len(rows)} companies\n"]
    if alerts:
        lines.append("\n## New this run\n")
        for name, p in alerts:
            lines.append(f"- **{name}**: [{p['title']}]({p['url']}) {p['location']}")
    group = None
    for g, name, status, kind, total, interns, note in rows:
        if g != group:
            group = g
            lines += [f"\n## {g}\n", "| Company | Status | System | Open engineering internships | Note |",
                      "|---|---|---|---|---|"]
        icon = {"ok": "✅", "skipped": "⏭️"}.get(status, "❌")
        lines.append(f"| {name} | {icon} {status} | {kind} | {interns if status == 'ok' else ''} "
                     f"| {note.replace('|', '/')[:120]} |")
    with open(STATUS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
