"""Decide what to do with a newly discovered posting.

Everything here is a pure function so it can be tested without the network.

Vocabulary (kept separate on purpose):
  first_seen   when this checker first saw the requisition (discovery time)
  posted       the date the employer's site reports. It is employer-reported:
               a refreshed posting can show today's date on an older role.
  updated      the employer's "last updated" value, never used as a posted date
"""
import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:                      # tz database missing: fall back to CST
    LOCAL_TZ = timezone(timedelta(hours=-6))

MODES = ("today", "last_48_hours", "newly_discovered")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ------------------------------------------------------------------ dates

def parse_posted(raw, now=None):
    """Parse an employer-reported date.

    Returns {"when": aware datetime | None, "day": local date | None,
             "precision": "timestamp" | "date" | "relative" | None}.
    A date-only value is never given a made-up time of day.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(LOCAL_TZ).date()
    t = str(raw or "").strip()
    low = t.lower()
    none = {"when": None, "day": None, "precision": None}
    if not t:
        return none
    if re.fullmatch(r"\d{12,14}", t):                               # epoch milliseconds
        when = datetime.fromtimestamp(int(t) / 1000, timezone.utc)
        return {"when": when, "day": when.astimezone(LOCAL_TZ).date(), "precision": "timestamp"}
    if re.fullmatch(r"\d{9,11}", t):                                # epoch seconds
        when = datetime.fromtimestamp(int(t), timezone.utc)
        return {"when": when, "day": when.astimezone(LOCAL_TZ).date(), "precision": "timestamp"}
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*"
                 r"(z|[+-]\d{2}:?\d{2})?)?", low)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        if m[4] is None or (m[4], m[5], m[6] or "00") == ("00", "00", "00"):
            # date only (a midnight timestamp is a date, not a real time of day)
            return {"when": None, "day": date(y, mo, d), "precision": "date"}
        off = m[7]
        if off in (None, "z"):
            tz = timezone.utc
        else:
            sign = 1 if off[0] == "+" else -1
            digits = off[1:].replace(":", "")
            tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        when = datetime(y, mo, d, int(m[4]), int(m[5]), int(m[6] or 0), tzinfo=tz)
        return {"when": when, "day": when.astimezone(LOCAL_TZ).date(), "precision": "timestamp"}
    if "today" in low or "just posted" in low or re.search(r"\bhours?\b|\bminutes?\b", low):
        return {"when": None, "day": today, "precision": "relative"}
    if "yesterday" in low:
        return {"when": None, "day": today - timedelta(days=1), "precision": "relative"}
    m = re.search(r"(\d+)\+?\s*days?", low)                         # "Posted 5 Days Ago", "30+ Days"
    if m:
        return {"when": None, "day": today - timedelta(days=int(m[1])), "precision": "relative"}
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", low)   # "Nov 3, 2026"
    if m and m[1] in MONTHS:
        return {"when": None, "day": date(int(m[3]), MONTHS[m[1]], int(m[2])), "precision": "date"}
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})", low)   # "3 Nov 2026"
    if m and m[2] in MONTHS:
        return {"when": None, "day": date(int(m[3]), MONTHS[m[2]], int(m[1])), "precision": "date"}
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", low)              # US style 11/10/2026
    if m:
        try:
            return {"when": None, "day": date(int(m[3]), int(m[1]), int(m[2])), "precision": "date"}
        except ValueError:
            return none
    return none


def recency(posted_raw, mode, now=None):
    """Return "recent", "old" or "unknown" for an employer-reported date.

    today           the employer date is today's date in America/Chicago
    last_48_hours   a timestamp within 48 hours; a date-only value counts when
                    it is today or yesterday (Chicago)
    newly_discovered  anything this checker has not seen before (date ignored)
    """
    if mode == "newly_discovered":
        return "recent"
    now = now or datetime.now(timezone.utc)
    p = parse_posted(posted_raw, now)
    if p["day"] is None:
        return "unknown"
    today = now.astimezone(LOCAL_TZ).date()
    if p["day"] > today + timedelta(days=1):
        return "unknown"                       # a future date is not evidence of anything
    if mode == "today":
        return "recent" if p["day"] >= today else "old"
    if p["when"] is not None:
        return "recent" if now - p["when"] <= timedelta(hours=48) else "old"
    return "recent" if p["day"] >= today - timedelta(days=1) else "old"


def describe_date(posted_raw, source, now=None):
    p = parse_posted(posted_raw, now)
    if p["day"] is None:
        return "date unknown"
    label = {"first_published": "employer first_published"}.get(source, "employer-reported")
    if p["when"] is not None:
        return f"{label} {p['when'].astimezone(LOCAL_TZ).strftime('%b %d %I:%M %p CT')}"
    return f"{label} {p['day'].strftime('%b %d')}"


# ------------------------------------------------------------------ country

US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
             "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
                  "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
                  "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
                  "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
                  "oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
                  "texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming")
CA_PROVINCES = "QC ON BC AB MB SK NS NB NL PE YT NT NU".split()
US_COUNTRY = re.compile(r"\b(united states( of america)?|u\.s\.a?\.?|usa)\b|(?:^|[,\s(/-])US(?:$|[,\s)/-])", re.I)
CANADA = re.compile(r"\b(canada|can)\b|\b(ontario|quebec|québec|british columbia|alberta|manitoba|"
                    r"saskatchewan|nova scotia|new brunswick)\b", re.I)
FOREIGN = re.compile(
    r"\b(india|china|germany|deutschland|france|uk|united kingdom|england|scotland|ireland|mexico|"
    r"méxico|brazil|brasil|japan|korea|singapore|malaysia|philippines|vietnam|thailand|indonesia|"
    r"taiwan|australia|new zealand|poland|czech|hungary|romania|slovakia|italy|italia|spain|españa|"
    r"portugal|netherlands|belgium|switzerland|austria|sweden|norway|denmark|finland|israel|turkey|"
    r"türkiye|saudi|uae|dubai|qatar|egypt|morocco|south africa|argentina|chile|colombia|peru|"
    r"costa rica|bangalore|bengaluru|hyderabad|pune|chennai|shanghai|suzhou|beijing|shenzhen|munich|"
    r"münchen|berlin|stuttgart|toulouse|paris|hamburg|bremen|madrid|getafe|montreal|montréal|toronto|"
    r"mississauga|querétaro|queretaro|monterrey|chihuahua|juarez|juárez|tijuana|guadalajara|krakow|"
    r"kraków|wroclaw|wrocław|rzeszow|rzeszów|budapest|bucharest|bratislava|prague|brno|eindhoven|"
    r"veldhoven|villach|amsterdam|rotterdam|kuala lumpur|bahamas|basildon|belo horizonte|dorval|"
    r"st-laurent|saint-laurent|newmarket)\b", re.I)
ISO_NON_US = re.compile(r"(?:^|[,\s(/-])(CAN|GBR|DEU|FRA|MEX|IND|CHN|BRA|JPN|KOR|SGP|AUS|NLD|ITA|ESP|POL|"
                        r"AUT|CHE|SWE|IRL|ISR)(?:$|[,\s)/-])")


def country_of(location, country=""):
    """Return "US", "non-US" or "unknown".

    A structured country field wins. Otherwise the location text is read with
    context: "CA" alone means California only when nothing says Canada.
    "Remote" never overrides an explicit foreign country, and remote with no
    country is unknown.
    """
    c = (country or "").strip()
    if c:
        if re.fullmatch(r"(?i)us|usa|united states( of america)?|u\.s\.a?\.?", c):
            return "US"
        return "non-US"
    loc = (location or "").strip()
    if not loc:
        return "unknown"
    if CANADA.search(loc) or ISO_NON_US.search(loc) or FOREIGN.search(loc):
        # e.g. "St-Laurent, QC, CAN", "Newmarket, ON, Canada", "Remote - Canada"
        if US_COUNTRY.search(loc) and not (CANADA.search(loc) or ISO_NON_US.search(loc)):
            return "US"                              # "Paris, Texas, United States"
        return "non-US"
    tokens = re.findall(r"[A-Za-z]+", loc)
    if any(t in CA_PROVINCES for t in tokens if t != "CA") and not US_COUNTRY.search(loc):
        if not any(t in US_STATES for t in tokens):
            return "non-US"
    if US_COUNTRY.search(loc):
        return "US"
    if any(t in US_STATES for t in tokens):          # ", TX" / "US-Kansas-Wichita" handled above
        return "US"
    if re.search(rf"\b({US_STATE_NAMES})\b", loc, re.I):
        return "US"
    return "unknown"                                  # "Remote", "3 Locations", a bare city


# ------------------------------------------------------------------ relevance

EXCLUDE_RULES = [
    ("social media role", r"social media"),
    ("technician or apprenticeship role", r"technician|apprentice|\bmechanic\b|machinist|assembler|"
                                          r"operator|\bwelder\b"),
    ("graduate-level only", r"\b(ph\.?d|doctoral|mba|masters|master's|ms/phd|graduate)\b"),
    ("civil engineering", r"\bcivil\b|construction management"),
    ("software or data role", r"software|\bit\b|information technology|information systems|\bdata\b|"
                              r"analytics|cyber|security|machine learning|\bml\b|\bai\b|"
                              r"artificial intelligence|computer science|developer|devops|\bweb\b|"
                              r"cloud|firmware|embedded|network"),
    ("business role", r"finance|financial|accounting|\bhr\b|human resource|people|talent|recruit|"
                      r"marketing|sales|supply chain|procurement|purchasing|sourcing|logistics|legal|"
                      r"\blaw\b|communications|public relations|business|strategy|brand|"
                      r"interior design|graphic|\bux\b|\bui\b|user experience|product manage|"
                      r"program manage|project manage|government relations|policy|"
                      r"customer experience|events|executive|administrative|office|audit|\btax\b|"
                      r"treasury|investor|real estate|\behs\b|health|safety|sustainab|"
                      r"technical writ|documentation"),
]
STRONG = [
    ("mechanical", r"mechanical|mechanism|mechatronic"),
    ("aerospace hardware", r"aerospace|aeronautic|astronautic|propulsion|structures\b|stress|airframe|"
                           r"loads|aerodynamic|thermal|fluid|spacecraft|satellite|rocket|launch|"
                           r"aircraft|flight|ground support|\bgse\b|engine\b|turbo|payload"),
    ("design / product development", r"design engineer|product development|product engineer|"
                                     r"product design|\bcad\b|r&d|research and development|"
                                     r"powertrain|chassis|vehicle|drivetrain"),
    ("robotics hardware", r"robotic|hardware"),
]
FIT = [
    ("manufacturing", r"manufactur|production engineer|tooling|weld|machin|additive|composite|"
                      r"industrial engineer|continuous improvement|\blean\b|assembly"),
    ("test", r"\btest|validation|reliability|calibration"),
    ("process / quality / materials", r"process|quality|material|metallurg|\bm&p\b"),
    ("systems / controls", r"systems|integration|controls?\b|automation"),
    ("general engineering", r"\bengineering (intern|co-?op)|\bengineer (intern|co-?op)|"
                            r"^(intern|co-?op)[\s,:–-]+engineering\b|engineering$"),
]
REVIEW_DISC = re.compile(r"electrical|electronics|avionic|\brf\b|structural|facilities|maintenance|"
                         r"nuclear|mining|chemical|biomedical|optical|photonic|telecom|5g", re.I)
OTHER_ENG = re.compile(r"engineer|electrical|electronics|avionic|\brf\b|facilities|maintenance|"
                       r"energy|power|nuclear|mining|hydraulic|structural|\bgnc\b|guidance", re.I)


def relevance(title):
    """Return (label, reason). Labels: "strong fit", "fit", "needs review", "excluded".

    Title matching is a first pass only. It does not establish eligibility.
    """
    t = title or ""
    for reason, pat in EXCLUDE_RULES:
        if re.search(pat, t, re.I):
            return "excluded", reason
    for reason, pat in STRONG:
        if re.search(pat, t, re.I):
            return "strong fit", reason
    if REVIEW_DISC.search(t):
        return "needs review", "other engineering discipline"
    for reason, pat in FIT:
        if re.search(pat, t, re.I):
            return "fit", reason
    if OTHER_ENG.search(t):
        return "needs review", "other engineering discipline"
    return "excluded", "not an engineering title"


def requirement_flags(text):
    """Pull hard-requirement hints out of a job description. Labels only: nothing
    here decides whether the reader is eligible."""
    t = re.sub(r"\s+", " ", text or "")
    flags = []
    if re.search(r"u\.?s\.? citizen|united states citizen|\bitar\b|u\.?s\.? person|export control|"
                 r"lawful permanent resident", t, re.I):
        flags.append("citizenship/ITAR stated")
    if re.search(r"security clearance|secret clearance|top secret", t, re.I):
        flags.append("clearance mentioned")
    if re.search(r"(pursuing|enrolled in|working toward)[^.]{0,40}(master|ph\.?d|doctoral|graduate degree)",
                 t, re.I) and not re.search(r"bachelor|undergraduate|\bbs\b|b\.s\.", t, re.I):
        flags.append("graduate students only?")
    m = re.search(r"(graduat\w+ (date|between|in|by|no earlier|after|before)[^.;]{0,70})", t, re.I)
    if m:
        flags.append("graduation: " + m.group(1).strip()[:90])
    m = re.search(r"((minimum|cumulative|overall)[^.;]{0,20}gpa[^.;]{0,25}|gpa of [0-9.]+[^.;]{0,15})", t, re.I)
    if m:
        flags.append(m.group(1).strip()[:60])
    if re.search(r"(no|not eligible for|does not (offer|provide)|without) [^.]{0,30}(relocation|housing)",
                 t, re.I):
        flags.append("housing/relocation: unavailable")
    elif re.search(r"(relocation|housing)[^.]{0,40}(assistance|stipend|provided|offered|support|available)",
                   t, re.I):
        flags.append("housing/relocation: offered")
    else:
        flags.append("housing/relocation: unstated")
    return flags


# ------------------------------------------------------------------ page validation

ERROR_MARKERS = re.compile(
    r"page (you (requested|are looking for)|not found|cannot be found)|job (is )?no longer (available|"
    r"posted|accepting)|position has been (filled|closed)|this job (has expired|is closed|has been "
    r"removed)|posting (has )?(expired|closed)|an error (has )?occurred|error page|exception|"
    r"no longer accepting applications|job not found|404", re.I)


def page_status(page_html, title, req_id=""):
    """Classify a fetched job page as "open", "closed" or "unverified".

    An HTTP 200 is not enough: an error page served with 200 is not an open job.
    The page has to show this requisition (its id or most of its title words)
    and an application action.
    """
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    low = text.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 3]
    hits = sum(1 for w in words if w in low)
    has_job = (req_id and str(req_id).lower() in low) or (words and hits >= max(2, len(words) * 0.6))
    head = low[:1500]
    if not has_job:
        return "closed" if ERROR_MARKERS.search(head) else "unverified"
    if re.search(r"no longer accepting applications|position has been (filled|closed)|"
                 r"job (is )?no longer (available|posted)", low):
        return "closed"
    if re.search(r"\bapply\b", low):
        return "open"
    return "unverified"


# ------------------------------------------------------------------ decision

def classify(p, mode, now=None, handled=()):
    """Return (bucket, reasons) where bucket is "alert", "review" or "skip".

    p needs: title, location, posted, and optionally country, date_source,
    status ("open"/"closed"/"unverified"), id.
    """
    label, why = relevance(p.get("title", ""))
    if label == "excluded":
        return "skip", [f"excluded: {why}"]
    if p.get("id") and str(p["id"]) in handled:
        return "skip", ["already handled"]
    where = country_of(p.get("location", ""), p.get("country", ""))
    if where == "non-US":
        return "skip", ["outside the US"]
    if p.get("status") == "closed":
        return "skip", ["posting is closed or an error page"]
    when = recency(p.get("posted", ""), mode, now)
    if when == "old":
        return "skip", ["employer date is older than the alert window"]
    reasons = []
    if when == "unknown":
        reasons.append("posted date unknown")
    if where == "unknown":
        reasons.append("country unknown")
    if label == "needs review":
        reasons.append(why)
    if p.get("status") == "unverified":
        reasons.append("could not verify the posting page")
    return ("review" if reasons else "alert"), reasons
