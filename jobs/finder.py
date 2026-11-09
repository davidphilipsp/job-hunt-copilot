"""Sniper job finder: fetch → filter → lane → score → out/YYYY-MM-DD.md.

Sources are public APIs/RSS only (Remotive, Himalayas, We Work Remotely, Get on Board, Remote OK, Jobicy,
Working Nomads) plus company career pages via public ATS feeds (jobs/watchlist.py).
Never scrapes LinkedIn, never applies anywhere.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402  (personal values come from config.yaml, see config.example.yaml)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "out"
SEEN_FILE = DATA / "seen.json"
SONAR_FILE = DATA / "sonar.json"         # every job that passed, for the sonar page
APPS_FILE = DATA / "applications.json"   # saved / applied / hidden, written by the sonar page
UA = {"User-Agent": "job-hunt-copilot/1.0 (personal job search; public feeds only)"}
NOW = datetime.now(timezone.utc)

# Search criteria: personal values live in config.yaml (see config.example.yaml). Amounts are monthly, home currency.
PAY_FLOOR_BRL_MONTH = int(settings.get("pay.floor_month", 0))          # below this → pay 0
PAY_TARGET_BRL_MONTH = int(settings.get("pay.target_month", 0))        # top of the range; ask here
AD_PAY_FLOOR_BRL_MONTH = int(settings.get("pay.agency_floor_month", 0) or PAY_FLOOR_BRL_MONTH)  # agency roles
CUR_SYM = settings.get("pay.currency_symbol", "R$")
PUB_DIR = OUT / "agency"
MAX_AGE_DAYS = int(os.environ.get("JOBHUNT_MAX_AGE_DAYS", "14"))  # older postings are dropped (likely filled or stale)
USD_PER_BRL = float(os.environ.get("JOBHUNT_USD_PER_BRL", settings.get("pay.usd_per_home_currency", 0.18)))  # update when FX moves

HIMALAYAS_QUERIES = [  # paid media + CRM, any industry
    "media buyer", "paid social", "paid media", "performance marketing", "meta ads", "facebook ads",
    "google ads", "tiktok ads", "user acquisition", "acquisition marketing", "growth marketing",
    "crm manager", "crm marketing", "lifecycle marketing", "retention marketing", "email marketing",
    "loyalty", "vip manager", "retention manager", "crm specialist", "klaviyo", "braze", "customer engagement",
    "advertising agency", "media planner", "media manager agency", "paid media agency", "digital agency",
    "igaming marketing", "latam marketing", "brazil marketing", "marketing portuguese", "marketing spanish",
    # lane D: dev/AI close to marketing
    "growth engineer", "marketing engineer", "marketing technologist", "martech", "crm developer",
    "lifecycle engineer", "ai automation", "automation engineer", "marketing automation developer",
]

# ---------------------------------------------------------------- model


@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    location: str
    description: str
    published: datetime | None
    salary_text: str = ""
    salary_usd_year: tuple[float, float] | None = None  # (min, max)
    lane: str = ""
    score: float = 0
    breakdown: dict = field(default_factory=dict)
    edges: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    industry: str = ""
    kind: str = ""                 # Full-time / Contract / Part-time
    needs_remote_proof: bool = False  # company ATS feeds list office jobs too: require "remote" in title/location

    @property
    def key(self) -> str:
        return norm(re.sub(r"\(.*?\)", "", self.company)) + "|" + norm(self.title)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.company}\n{self.description}".lower()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def strip_html(s: str) -> str:
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


def get(url: str, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(2)
    raise RuntimeError(f"rate limited: {url}")


# ---------------------------------------------------------------- sources

FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66, "BRL": USD_PER_BRL, "MXN": 0.054}
PERIOD = {"annual": 1, "year": 1, "yearly": 1, "month": 12, "monthly": 12, "week": 52, "weekly": 52,
          "hour": 2000, "hourly": 2000, "day": 230, "daily": 230}


def fetch_remotar() -> list[Job]:
    """Remotar (remotar.com.br): Brazilian remote-jobs board with a public JSON API.
    Newest first; stop once two pages in a row are older than MAX_AGE_DAYS.
    Salaries come in cents of the stated currency. Hybrid listings are labeled so the remote filter drops them."""
    jobs, old_pages = [], 0
    cutoff = NOW - timedelta(days=MAX_AGE_DAYS)
    for page in range(1, 41):
        try:
            data = get("https://api.remotar.com.br/jobs", params={"page": page}).json().get("data", [])
        except Exception as e:
            print(f"  remotar page {page} failed: {e}", file=sys.stderr)
            break
        if not data:
            break
        fresh_here = 0
        for j in data:
            pub = parse_iso(j.get("createdAt"))
            if j.get("expired") or not j.get("active", True) or (pub and pub < cutoff):
                continue
            fresh_here += 1
            sal, sal_usd, sal_text = j.get("jobSalary") or {}, None, ""
            if sal.get("to") and sal.get("currency") in FX and sal.get("type") in PERIOD:
                lo, hi = (sal.get("from") or sal["to"]) / 100, sal["to"] / 100
                k = FX[sal["currency"]] * PERIOD[sal["type"]]
                sal_usd, sal_text = (lo * k, hi * k), f"{sal['currency']} {lo:,.0f}–{hi:,.0f} / {sal['type']}"
            company = (j.get("company") or {}).get("name") or j.get("companyDisplayName") or ""
            jobs.append(Job(
                source="Remotar", title=j.get("title", ""), company=company,
                url=j.get("externalLink") if j.get("isExternalLink") and j.get("externalLink") else f"https://remotar.com.br/job/{j['id']}",
                location="Brasil" if j.get("type") == "remote" else "Brasil (híbrido)",
                description=strip_html(j.get("description") or ""), published=pub,
                salary_text=sal_text, salary_usd_year=sal_usd,
            ))
        old_pages = 0 if fresh_here else old_pages + 1
        if old_pages >= 2:
            break
        time.sleep(0.5)
    return jobs


def fetch_remotive() -> list[Job]:
    jobs, seen_url = [], set()
    for cat in ("marketing", "software-dev"):  # software-dev feeds lane D
        try:
            r = get("https://remotive.com/api/remote-jobs", params={"category": cat})
        except Exception as e:
            print(f"  remotive '{cat}' failed: {e}", file=sys.stderr)
            continue
        for j in r.json().get("jobs", []):
            if j["url"] in seen_url:
                continue
            seen_url.add(j["url"])
            jobs.append(Job(
                source="Remotive", title=j["title"], company=j["company_name"], url=j["url"],
                location=j.get("candidate_required_location") or "",
                description=strip_html(j.get("description", "")),
                published=parse_iso(j.get("publication_date")),
                salary_text=j.get("salary") or "",
            ))
    for job in jobs:
        job.salary_usd_year = parse_salary_text(job.salary_text)
    return jobs


def fetch_himalayas() -> list[Job]:
    jobs, seen_guid = [], set()
    for q in HIMALAYAS_QUERIES:
        try:
            r = get("https://himalayas.app/jobs/api/search", params={"q": q, "sort": "recent"})
        except Exception as e:
            print(f"  himalayas '{q}' failed: {e}", file=sys.stderr)
            continue
        for j in r.json().get("jobs", []):
            guid = j.get("guid") or j.get("applicationLink")
            if guid in seen_guid:
                continue
            seen_guid.add(guid)
            restr = j.get("locationRestrictions") or []
            sal = None
            if j.get("minSalary") or j.get("maxSalary"):
                mult = PERIOD.get((j.get("salaryPeriod") or "annual").lower(), 1)
                fx = FX.get((j.get("currency") or "USD").upper())
                if fx:
                    lo = (j.get("minSalary") or j.get("maxSalary")) * mult * fx
                    hi = (j.get("maxSalary") or j.get("minSalary")) * mult * fx
                    sal = (lo, hi)
            sal_text = ""
            if sal:
                sal_text = f"{j.get('currency') or 'USD'} {j.get('minSalary') or ''}–{j.get('maxSalary') or ''} / {j.get('salaryPeriod') or 'annual'}"
            jobs.append(Job(
                source="Himalayas", title=j["title"], company=j["companyName"],
                url=guid or "", location=", ".join(restr) if restr else "Worldwide",
                description=strip_html(j.get("description", "")),
                published=datetime.fromtimestamp(j["pubDate"], timezone.utc) if j.get("pubDate") else None,
                salary_text=sal_text, salary_usd_year=sal,
            ))
        time.sleep(0.7)
    return jobs


def fetch_wwr() -> list[Job]:
    r = get("https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss")
    root = ET.fromstring(r.content)
    jobs = []
    for it in root.iter("item"):
        t = it.findtext("title") or ""
        company, _, title = t.partition(": ")
        desc = strip_html(it.findtext("description") or "")
        pub = it.findtext("pubDate")
        jobs.append(Job(
            source="We Work Remotely", title=title or t, company=company if title else "",
            url=it.findtext("link") or "", location=(it.findtext("region") or "").strip(),
            description=desc, published=parsedate_to_datetime(pub) if pub else None,
        ))
    for job in jobs:
        job.salary_usd_year = parse_salary_text(job.description[:2000], strict=True)
    return jobs


GETONBOARD_QUERIES = ["agencia de marketing", "gestor de midia", "midia digital", "publicidade", "paid media", "media buyer", "performance marketing", "trafego pago", "meta ads", "google ads",
                      "growth marketing", "crm", "lifecycle", "retention", "email marketing", "user acquisition",
                      "growth engineer", "automatizacion", "automacao", "crm developer"]  # last 4: lane D


def fetch_getonboard() -> list[Job]:
    """Get on Board (LatAm board). Salary is monthly USD; remote_local = remote within listed countries."""
    jobs, seen_id = [], set()
    for q in GETONBOARD_QUERIES:
        try:
            r = get("https://www.getonbrd.com/api/v0/search/jobs",
                    params={"query": q, "per_page": 50, "expand": '["company"]'})
        except Exception as e:
            print(f"  getonboard '{q}' failed: {e}", file=sys.stderr)
            continue
        for j in r.json().get("data", []):
            if j["id"] in seen_id:
                continue
            seen_id.add(j["id"])
            a = j["attributes"]
            mod = a.get("remote_modality") or ""
            if not a.get("remote") or mod not in ("fully_remote", "remote_local"):
                loc = "hybrid / on-site"
            else:
                countries = [c for c in a.get("countries") or [] if c != "Remote"]
                loc = "Worldwide" if mod == "fully_remote" and not countries else ", ".join(countries) or "Remote"
            lo, hi = a.get("min_salary"), a.get("max_salary")
            sal = (12 * (lo or hi), 12 * (hi or lo)) if (lo or hi) else None
            company = ((a.get("company") or {}).get("data") or {}).get("attributes", {}).get("name", "")
            desc = "\n".join(strip_html(a.get(k) or "") for k in ("description", "projects", "functions", "desirable"))
            jobs.append(Job(
                source="Get on Board", title=a["title"], company=company, url=j["links"]["public_url"],
                location=loc, description=desc,
                published=datetime.fromtimestamp(a["published_at"], timezone.utc) if a.get("published_at") else None,
                salary_text=f"US$ {lo or ''}–{hi or ''} / month" if sal else "", salary_usd_year=sal,
            ))
        time.sleep(0.5)
    return jobs


def fetch_remoteok() -> list[Job]:
    """Remote OK API. Terms: credit Remote OK and link to its job page (the report links to it)."""
    r = get("https://remoteok.com/api", params={"tag": "marketing"})
    jobs = []
    for j in r.json():
        if not j.get("position"):
            continue  # first item is the legal notice
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        jobs.append(Job(
            source="Remote OK", title=j["position"], company=j.get("company", ""), url=j.get("url", ""),
            location=j.get("location") or "", description=strip_html(j.get("description", "")),
            published=parse_iso(j.get("date")),
            salary_text=f"US$ {lo:,}–{hi:,} / year" if hi else "", salary_usd_year=(lo or hi, hi) if hi else None,
        ))
    return jobs


def fetch_jobicy() -> list[Job]:
    """Jobicy API. Terms: credit Jobicy with a link to the job (the report links to it)."""
    r = get("https://jobicy.com/api/v2/remote-jobs", params={"count": 100, "industry": "marketing"})
    jobs = []
    for j in r.json().get("jobs", []):
        sal, sal_text = None, ""
        fx = FX.get((j.get("salaryCurrency") or "").upper())
        mult = PERIOD.get((j.get("salaryPeriod") or "").lower())
        if fx and mult and (j.get("salaryMin") or j.get("salaryMax")):
            lo, hi = j.get("salaryMin") or j.get("salaryMax"), j.get("salaryMax") or j.get("salaryMin")
            sal = (lo * mult * fx, hi * mult * fx)
            sal_text = f"{j['salaryCurrency']} {lo:,}–{hi:,} / {j['salaryPeriod']}"
        jobs.append(Job(
            source="Jobicy", title=html.unescape(j["jobTitle"]), company=j.get("companyName", ""), url=j["url"],
            location=j.get("jobGeo") or "", description=strip_html(j.get("jobDescription", "")),
            published=parse_iso(j.get("pubDate")), salary_text=sal_text, salary_usd_year=sal,
            kind=", ".join(j.get("jobType") or []),
        ))
    return jobs


def fetch_workingnomads() -> list[Job]:
    r = get("https://www.workingnomads.com/api/exposed_jobs/")
    jobs = []
    for j in r.json():
        desc = strip_html(j.get("description", ""))
        jobs.append(Job(
            source="Working Nomads", title=j["title"], company=j.get("company_name", ""), url=j["url"],
            location=j.get("location") or "", description=desc, published=parse_iso(j.get("pub_date")),
            salary_usd_year=parse_salary_text(desc[:2000], strict=True),
        ))
    return jobs


def parse_iso(s):
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


SAL_RE = re.compile(
    r"(?P<cur>US\$|USD|\$|€|EUR|£|GBP|R\$|BRL)\s?(?P<a>\d[\d.,]*)\s?(?P<ak>k)?"
    r"(?:\s?(?:-|–|to)\s?(?:US\$|USD|\$|€|£|R\$)?\s?(?P<b>\d[\d.,]*)\s?(?P<bk>k)?)?"
    r"(?:\s?(?:/|per|a)\s?(?P<per>year|yr|annum|month|mo|hour|hr|week|day))?",
    re.I,
)
CUR = {"US$": "USD", "$": "USD", "USD": "USD", "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP", "R$": "BRL", "BRL": "BRL"}
PER = {"year": "annual", "yr": "annual", "annum": "annual", "month": "month", "mo": "month",
       "hour": "hour", "hr": "hour", "week": "week", "day": "day"}


def _num(s: str, k: bool) -> float:
    s = s.replace(",", "")
    if s.count(".") > 1:
        s = s.replace(".", "")
    v = float(s)
    return v * 1000 if k else v


def parse_salary_text(text: str, strict: bool = False) -> tuple[float, float] | None:
    """Return (min,max) annual USD from free text, or None if no clear salary."""
    if not text or re.search(r"per task|commission only|OTE", text, re.I):
        return None
    for m in SAL_RE.finditer(text):
        try:
            a = _num(m["a"], bool(m["ak"]))
            b = _num(m["b"], bool(m["bk"] or (m["ak"] and m["b"] and len(m["b"]) <= 3))) if m["b"] else a
        except ValueError:
            continue
        if m["ak"] and m["b"] and not m["bk"] and b < 1000:
            b *= 1000
        per = PER.get((m["per"] or "").lower())
        if not per:
            if a >= 15000:
                per = "annual"
            elif strict:
                continue
            elif a >= 1500:
                per = "month"
            elif a < 300:
                per = "hour"
            else:
                continue
        mult = PERIOD[per]
        fx = FX[CUR[m["cur"].upper() if m["cur"].upper() in CUR else m["cur"]]]
        lo, hi = a * mult * fx, b * mult * fx
        if 5000 <= hi <= 1_000_000:
            return (lo, hi)
    return None


# ---------------------------------------------------------------- filters

OK_LOC = re.compile(r"worldwide|anywhere|global|brazil|brasil|latam|latin america|south america|americas|"
                    r"\bamer\b|^$|remote$", re.I)
BAD_RESIDENCY = re.compile(
    r"must (?:reside|live|be (?:located|based)) in (?:the )?(?:us|u\.s\.|united states|uk|europe|eu|canada|germany)|"
    r"(?:authori[sz]ed|eligible|legally able) to work in (?:the )?(?:us|u\.s\.|united states|uk|eu|europe|canada)|"
    r"us (?:residents|citizens) only|eu (?:residents|work permit)|right to work in the uk|"
    r"(?:us|eu|uk)[- ]based (?:candidates )?only|no visa sponsorship.*(?:us|uk)",
    re.I,
)
OTHER_LANG = r"(?:german|french|italian|dutch|polish|swedish|danish|norwegian|finnish|japanese|korean|mandarin|chinese|turkish|russian|arabic|hebrew|greek|czech|hungarian|romanian)"
FLUENT_OTHER = re.compile(
    rf"(?:fluent|fluency|native|proficien\w*|business[- ]level|c1|c2)[^.\n]{{0,40}}\b{OTHER_LANG}\b|"
    rf"\b{OTHER_LANG}\b[^.\n]{{0,25}}(?:fluency|fluent|native|required|is a must|mandatory|c1|c2)",
    re.I,
)
GERMAN_WORDS = re.compile(r"\b(und|wir|für|mit|der|die|das|sie|deine?|unser)\b", re.I)
NOT_REMOTE = re.compile(
    r"\bhybrid\b|h[íi]brido|on-?site (?:role|position|job)|in-office|office[- ]based|presencial|"
    r"\d days? (?:a|per) week in (?:the|our) office|relocat(?:e|ion) (?:to|is required)",
    re.I,
)
HYBRID_OK = settings.regex("location.hybrid_ok_regex")  # metro area where hybrid / on-site is acceptable (config)
HARD_CORE = re.compile(r"amazon|programmatic|\bdsp\b|\bctv\b|retail media", re.I)


REMOTE_PROOF = re.compile(r"remote|remoto|anywhere|work from home|home[- ]office|teletrabajo|distributed", re.I)
TITLE_OPEN = re.compile(r"latam|latin america|south america|brazil|brasil|remote global|worldwide|anywhere", re.I)


# National (Brazil) roles: a Brazilian company page often lists the city and says "100% remoto" only in the body.
BR_LOC = re.compile(r"bra[sz]il|\b(?:sp|rj|mg|pr|sc|rs|pe|ba|df)\b|s[ãa]o paulo|rio de janeiro|belo horizonte|curitiba|"
                    r"florian[óo]polis|porto alegre|recife|salvador|bras[íi]lia", re.I)
FULL_REMOTE_BODY = re.compile(r"100% remot|totalmente remot|trabalho remoto|modelo (?:de trabalho )?remoto|"
                              r"home[- ]office integral|fully remote|remote[- ]first", re.I)


def location_ok(job: Job) -> tuple[bool, str]:
    loc = job.location.strip()
    br_remote = bool(BR_LOC.search(loc) and FULL_REMOTE_BODY.search(job.description))
    if job.needs_remote_proof and not REMOTE_PROOF.search(f"{loc} {job.title}") and not br_remote \
            and not HYBRID_OK.search(loc):
        return False, f"office job (company page): {loc or 'no location'}"
    # Himalayas sometimes tags a "LATAM only" job with one country; the title is the clearer signal.
    if loc and not OK_LOC.search(loc) and not TITLE_OPEN.search(job.title):
        return False, f"location: {loc}"
    m = BAD_RESIDENCY.search(job.description)
    if m:
        return False, f"residency: '{m.group(0)}'"
    return True, ""


# Advertising / agency context. Agency jobs get the agency pay floor and their own daily report.
AGENCY = re.compile(r"ag[êe]ncia|\bagency\b|assessoria de marketing|publicidade|propaganda|\badvertising\b|"
                    r"multi-?client|client accounts|carteira de clientes|contas de clientes", re.I)
PT_POSTING = re.compile(r"\b(?:você|experiência|vaga|requisitos|benefícios|atividades|responsabilidades|conhecimento em)\b", re.I)


def is_agency(job: Job) -> bool:
    return bool(AGENCY.search(job.text))


OTHER_COUNTRY = re.compile(r"argentin|chile|colombia|m[ée]xic|per[uú]\b|urugu|paragu|bolivi|ecuador|venezu|costa rica|panam|"
                           r"guatemal|hondur|el salvador|nicaragu|dominican|puerto rico|cuba|belize|bahamas|barbados|anguilla|"
                           r"antigua|aruba|bermuda|jamaica|trinidad|guyan|surinam|united|canada|spain|portugal|algeria|angola", re.I)


def is_br(job: Job) -> bool:
    """Brazilian posting: written in Portuguese, Brazil in the title, or Brazil as the only country listed.
    A LATAM-wide list that happens to include Brazil is international, not national."""
    if len(PT_POSTING.findall(job.description[:3000])) >= 2 or re.search(r"\bbra[sz]il", job.title, re.I):
        return True
    return bool(BR_LOC.search(job.location)) and not OTHER_COUNTRY.search(job.location)


def pay_floor(job: Job) -> int:
    return AD_PAY_FLOOR_BRL_MONTH if is_agency(job) else PAY_FLOOR_BRL_MONTH


def hard_drop(job: Job) -> str:
    if job.published and NOW - job.published > timedelta(days=MAX_AGE_DAYS):
        return f"stale: posted {(NOW - job.published).days}d ago (> {MAX_AGE_DAYS}d)"
    m = FLUENT_OTHER.search(job.description) or FLUENT_OTHER.search(job.title)
    if m:
        return f"language: '{m.group(0).strip()}'"
    if "(m/w/d)" in job.title.lower() or len(GERMAN_WORDS.findall(job.description[:1500])) > 25:
        return "language: posting is in German"
    m = NOT_REMOTE.search(job.title) or NOT_REMOTE.search(job.location) or NOT_REMOTE.search(job.description)
    in_sp = bool(HYBRID_OK.search(job.location) or HYBRID_OK.search(job.title))  # candidate's own metro area
    if m and not in_sp and not re.search(r"not (?:a )?hybrid|no (?:office|relocation)", job.description, re.I):
        return f"not fully remote: '{m.group(0)}'"
    if job.salary_usd_year and brl_month(job.salary_usd_year[1]) < pay_floor(job):
        return f"pay below {CUR_SYM}{pay_floor(job):,}/month: {job.salary_text}"
    if HARD_CORE.search(job.title):
        return f"core is Amazon/programmatic: '{job.title}'"
    return ""


# ---------------------------------------------------------------- lanes & score

# Focus: paid media and CRM in any industry. Industry is a tag (+ edge if the candidate has worked in it), not a lane.
PAID = re.compile(r"media buy|paid social|paid media|performance market|meta ads|facebook ads|tiktok ads|google ads|"
                  r"user acquisition|paid acquisition|acquisition market|\bppc\b|\bsem\b|paid ads|paid growth|growth market|\bua\b|"
                  r"tr[áa]fego pago|gestor de tr[áa]fego|m[íi]dia paga|m[íi]dia de performance|marketing de performance|"
                  r"analista de performance|aquisi[çc][ãa]o|planejamento de m[íi]dia|analista de m[íi]dia|gestor(?:a)? de m[íi]dia|"
                  r"coordenador(?:a)? de m[íi]dia|m[íi]dia (?:digital|online|on-?line|programada)|media plann|media manager|"
                  r"medios pagos|marketing de rendimiento|adquisici[óo]n", re.I)
CRM = re.compile(r"\bcrm\b|lifecycle|retention|loyalty|\bvip\b|braze|customer\.io|optimove|smartico|klaviyo|"
                 r"email market|engagement market|customer marketing|reten[çc][ãa]o|retenci[óo]n|fideliza|relacionamento|"
                 r"ciclo de vida", re.I)
LANES = {
    "AC": "Paid media + CRM in one role",
    "A": "Paid media / performance",
    "C": "CRM / lifecycle / retention",
    "D": "Dev / AI for marketing & CRM",
}
INDUSTRIES = [  # (name, regex); which ones you worked in comes from config.yaml → worked_industries
    ("iGaming / betting", re.compile(r"igaming|i-gaming|sportsbook|casino|\bbetting\b|gambling|lottery|\bbingo\b|poker|sweepstakes", re.I)),
    ("Education / courses", re.compile(r"edtech|education|\bcourses?\b|students?|universit|e-?learning|bootcamp|enrol", re.I)),
    ("E-commerce / DTC", re.compile(r"e-?commerce|\bdtc\b|d2c|shopify|direct[- ]to[- ]consumer|online store", re.I)),
    ("Info products / launches", re.compile(r"info[- ]?products?|\bvsl\b|coaching program|digital products?|launches", re.I)),
    ("Fintech / trading", re.compile(r"fintech|\btrading\b|forex|crypto|brokerage|neobank|payments", re.I)),
    ("Health / wellness", re.compile(r"health|wellness|telehealth|clinic|supplement", re.I)),
    ("Apps / mobile", re.compile(r"mobile app|\bapps?\b|app store|subscription app", re.I)),
    ("B2B / SaaS", re.compile(r"\bb2b\b|\bsaas\b", re.I)),
]
IGAMING = INDUSTRIES[0][1]
WORKED_INDUSTRIES = set(settings.get("worked_industries", []) or [])
MARKETING_TITLE = re.compile(r"market|growth|acquisition|media|crm|lifecycle|retention|loyalty|\bvip\b|paid|ads\b|ppc|ua\b|"
                             r"engagement|customer|tr[áa]fego|m[íi]dia|aquisi|reten|performance", re.I)
# Titles whose core is not paid media or CRM, even when they mention them.
NOT_CORE = re.compile(r"copywriter|designer|editor|engineer|developer|recruiter|account executive|sales (?:manager|rep)|"
                      r"\bsdr\b|\bbdr\b|translator|moderator|data scientist|customer (?:service|success|support)|\bhr\b|"
                      r"product marketing|brand (?:manager|marketing|design)|content|\bseo\b|\bpr\b|communications|events?\b|"
                      r"community|social media manager|influencer|partnership|affiliate|mid-market|enterprise account", re.I)


# Lane D: software/AI roles whose core is marketing or CRM systems (growth/marketing engineer, CRM developer, AI automation).
DEV_TITLE = re.compile(
    r"growth engineer|marketing (?:engineer|technolog)|martech|marketing (?:automation|ops|operations) (?:engineer|developer)|"
    r"(?:crm|lifecycle|retention|loyalty|engagement) (?:engineer|developer|architect)|"
    r"ai automation|automation (?:engineer|developer)|ai (?:agents?|workflows?) (?:engineer|developer|builder)|"
    r"engenheir[oa] de growth|desenvolvedor(?:a)? (?:de )?crm|automa[çc][ãa]o de marketing|"
    r"ingenier[oa] de growth|desarrollador(?:a)? (?:de )?crm|automatizaci[óo]n de marketing", re.I)
GENERIC_DEV_TITLE = re.compile(r"engineer|developer|desenvolvedor|desarrollador|programador|full[- ]?stack|back[- ]?end", re.I)
MARTECH_BODY = re.compile(r"\bcrm\b|lifecycle|marketing|attribution|tracking|conversions? api|\bcapi\b|pixel|whatsapp|"
                          r"affiliate|loyalty|campaigns?|segment\w*|journeys?|braze|klaviyo|customer\.io|growth|ad platforms?", re.I)
# Stacks outside the candidate's evidence: a title built on them is not a fit (edit to match your profile).
UNPROVEN_TITLE = re.compile(r"\bjava\b|golang|\brust\b|\.net|\bc#|kotlin|swift|\bios\b|android|mobile|devops|\bsre\b|"
                            r"data engineer|ml engineer|machine learning|salesforce|\bapex\b|marketo|adobe|hubspot developer|embedded|firmware|\bqa\b|\btest", re.I)
STACK_MATCH = re.compile(r"node\.?js|\breact\b|postgres|python|fastify|playwright|\bmcp\b|claude|anthropic|llm", re.I)


def dev_lane(t: str, body: str) -> tuple[str, int]:
    if UNPROVEN_TITLE.search(t) or re.search(r"sales|recruit|data scientist|designer|business develop|developer (?:relations|advocate)|devrel", t, re.I):
        return "", 0
    if DEV_TITLE.search(t):
        return ("D", 30) if len(MARTECH_BODY.findall(body)) >= 2 else ("D", 15)
    if GENERIC_DEV_TITLE.search(t) and len({m.lower() for m in MARTECH_BODY.findall(body)}) >= 5:
        return "D", 15  # plain engineer title, but the job is clearly about marketing/CRM systems
    return "", 0


def industry_of(job: Job) -> tuple[str, bool]:
    counts = [(len(rx.findall(job.text)), name, name in WORKED_INDUSTRIES) for name, rx in INDUSTRIES]
    n, name, worked = max(counts)
    return (name, worked) if n >= 2 else ("Unclear", False)


def assign_lane(job: Job) -> tuple[str, int]:
    """Lane + lane-fit points: 30 if the title (core of the job) is paid/CRM, 15 if only the body is."""
    t, body = job.title, job.description
    if DEV_TITLE.search(t) or GENERIC_DEV_TITLE.search(t):
        return dev_lane(t, body)
    if NOT_CORE.search(t) or not MARKETING_TITLE.search(t):
        return "", 0
    paid_t, crm_t = bool(PAID.search(t)), bool(CRM.search(t))
    paid_b, crm_b = len(PAID.findall(body)), len(CRM.findall(body))
    if (paid_t or crm_t) and paid_b >= 2 and crm_b >= 2 or (paid_t and crm_t):
        return "AC", 30
    if paid_t:
        return "A", 30
    if crm_t:
        return "C", 30
    if paid_b >= 3 and crm_b >= 3:
        return "AC", 15
    if max(paid_b, crm_b) >= 2:
        return ("A" if paid_b >= crm_b else "C"), 15
    return "", 0


EDGES = [
    ("Conversions API / server-side", re.compile(r"conversions? api|\bcapi\b|server[- ]side|offline conversions", re.I)),
    ("GA4 / GTM", re.compile(r"\bga4\b|google analytics|\bgtm\b|tag manager", re.I)),
    ("Agency / advertising", AGENCY),
    ("Lead gen with SDR/sales team", re.compile(r"(?=.*lead[- ]gen)(?=.*(?:\bsdrs?\b|sales team|inside sales|call center|closers?)).*", re.I | re.S)),
    ("Industry I've worked in", None),  # computed from industry_of()
    ("Paid + CRM in one role", None),   # computed from the lane
    ("Lifecycle / loyalty / CRM tool", re.compile(r"lifecycle|loyalty|braze|customer\.io|optimove|smartico|klaviyo|\bvip\b", re.I)),
    ("AI agents / automation", re.compile(r"\bai agents?\b|\bllms?\b|claude|\bgpt|agentic|n8n|zapier|make\.com|ai[- ]first|ai[- ]powered|"
                                          r"generative ai|\bgenai\b|ai (?:tools|automation|workflows)", re.I)),
    ("Brazil / LatAm / Portuguese / Spanish", re.compile(r"brazil|brasil|latam|latin america|portuguese|português|spanish|español|espanhol", re.I)),
]
PENALTIES = [
    (-15, "LinkedIn / B2B ABM at scale", re.compile(r"account[- ]based|\babm\b|(?=.*linkedin ads)(?=.*\bb2b\b).*", re.I | re.S)),
    (-10, "B2B SaaS required", re.compile(r"b2b saas|saas b2b|b2b software", re.I)),
    (-10, "CS interview loop (LeetCode/algorithms/CS degree)", re.compile(
        r"leetcode|data structures and algorithms|algorithmic|live coding|computer science degree|(?:bs|ms|bachelor\S*) in computer science", re.I)),
    (-5, "Booked calls as primary KPI", re.compile(r"booked (?:calls|meetings|demos)|demos? booked|meetings? booked|sales[- ]qualified|\bsqls\b", re.I)),
]
BLUEPRINT = re.compile(r"[^.\n]{0,60}meta blueprint[^.\n]{0,60}", re.I)


def score(job: Job, lane_pts: int) -> None:
    b = {"lane": lane_pts}
    txt = job.text
    job.industry, worked = industry_of(job)
    job.edges = [name for name, rx in EDGES if rx and rx.search(txt)]
    if worked:
        job.edges.append("Industry I've worked in")
    if job.lane == "AC":
        job.edges.append("Paid + CRM in one role")
    if job.lane == "D" and STACK_MATCH.search(txt):
        job.edges.append("Stack match (Node/React/Postgres/Python/LLM)")
    b["edge"] = min(25, 5 * len(job.edges))

    if job.salary_usd_year:
        hi = job.salary_usd_year[1]
        b["pay"] = 20 if brl_month(hi) >= PAY_FLOOR_BRL_MONTH else 10 if brl_month(hi) >= pay_floor(job) else 0
    else:
        b["pay"] = 10
    if job.published:
        age = NOW - job.published
        # Newer postings are likelier to be live and still screening: steeper curve.
        b["fresh"] = (15 if age <= timedelta(hours=24) else 12 if age <= timedelta(hours=72) else
                      6 if age <= timedelta(days=7) else 2 if age <= timedelta(days=MAX_AGE_DAYS) else 0)
    else:
        b["fresh"] = 0

    t = job.title.lower()
    if re.search(r"intern|junior|\bjr\b|j[úu]nior|entry|assistant|associate|trainee|est[áa]gi", t):
        b["senior"] = 0
    elif re.search(r"director|diretor|head of|\bvp\b|vice president|chief", t):
        b["senior"] = 5
    elif re.search(r"senior|s[êe]nior|\bsr\b|lead|manager|gerente|coordenador|principal|staff", t):
        b["senior"] = 10
    else:
        b["senior"] = 5  # unstated level (e.g. "Media Buyer"): half

    pen = 0
    for pts, name, rx in PENALTIES:
        if rx.search(txt):
            pen += pts
            job.flags.append(name)
    if job.lane == "D" and re.search(r"\b(?:aws|gcp|azure|kubernetes|terraform)\b", txt, re.I) and \
            re.search(r"(?:required|must|strong|deep|expert)[^.\n]{0,40}\b(?:aws|gcp|azure|kubernetes|terraform)\b", txt, re.I):
        pen -= 5
        job.flags.append("Cloud infra required (unproven)")
    for m in BLUEPRINT.finditer(job.description):
        ctx = m.group(0).lower()
        if re.search(r"required|must|mandatory", ctx) and not re.search(r"prefer|plus|nice|bonus", ctx):
            pen -= 10
            job.flags.append("Meta Blueprint REQUIRED")
            break
    b["penalty"] = pen
    job.breakdown = b
    job.score = max(0, sum(b.values()))


# ---------------------------------------------------------------- profile mapping
# Proofs and gaps are personal facts: they live in config.yaml, never in code. No numeric results.
# Each proof tuple: (id, lanes, edges, job-text keywords, report line, first-person letter sentence)

_IND_ONLY = settings.get("industry_only_proofs", {}) or {}
INDUSTRY_ONLY_NAME = _IND_ONLY.get("industry", "")
IGAMING_ONLY = set(_IND_ONLY.get("ids", []) or [])          # only lead with these when the job is in that industry
REPLACED_IN_INDUSTRY = set(_IND_ONLY.get("replaces", []) or [])  # skipped there (a more specific proof is used)

PROOFS = [
    (p["id"], set(p.get("lanes", []) or []), set(p.get("edges", []) or []), p.get("keywords", r"(?!x)x"),
     p.get("line", ""), p.get("sentence", ""))
    for p in settings.get("proofs", []) or []
]


def _gaps(key: str) -> list[tuple]:
    return [(re.compile(g["regex"], re.I), g["name"], g["answer"]) for g in settings.get(key, []) or []]


GAPS = _gaps("gaps")


def lead_with(job: Job) -> list[tuple]:
    """Two strongest PROFILE proofs for this job: lane match + edge match + keyword hits in the posting."""
    ranked = []
    for i, p in enumerate(PROOFS):
        pid, lanes, edges, kw, _, _ = p
        if pid in IGAMING_ONLY and job.industry != INDUSTRY_ONLY_NAME:
            continue
        if pid in REPLACED_IN_INDUSTRY and job.industry == INDUSTRY_ONLY_NAME:
            continue  # use the industry-specific proof instead
        hits = len(set(m.lower() for m in re.findall(kw, job.text, re.I)))
        in_title = 4 if re.search(kw, job.title, re.I) else 0  # the title is the core of the job
        s = (3 if job.lane in lanes else 0) + 2 * len(edges & set(job.edges)) + min(hits, 4) + in_title
        ranked.append((s, -i, p))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [p for _, _, p in ranked[:2]]


DEV_GAPS = _gaps("dev_gaps")
DEFAULT_PREP = settings.get("default_prep", {}) or {}


def gap_for(job: Job) -> tuple[str, str]:
    for rx, name, answer in (DEV_GAPS + GAPS if job.lane == "D" else GAPS):
        if rx.search(job.text):
            return name, answer
    if job.lane == "D":
        return "Nothing explicit in the posting", DEFAULT_PREP.get("dev", "Prep anyway: walk through one system you built end to end.")
    return "Nothing explicit in the posting", DEFAULT_PREP.get("other", "Prep anyway: walk through one project end to end.")


# ---------------------------------------------------------------- cover letters

BANNED = [w.lower() for w in settings.get("banned_phrases", []) or []]
NUMERIC_RESULT = re.compile(r"\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?x\b|roas of|uplift of|increased .* by \d|reduced .* by \d", re.I)
# Approved scope numbers (config.yaml → allowed_numbers). Any other digit in a draft is flagged.
ALLOWED_NUMBERS = re.compile("|".join(settings.get("allowed_numbers", []) or [r"(?!x)x"]), re.I)


def letter_ok(text: str, job: Job | None = None) -> list[str]:
    problems = [f"banned phrase '{w}'" for w in BANNED if w in text.lower()]
    if NUMERIC_RESULT.search(text):
        problems.append("numeric result")
    stripped = ALLOWED_NUMBERS.sub("", text)
    if job:  # digits inside the job title/company name are not claims
        stripped = stripped.replace(job.title, "").replace(job.company, "")
    if re.search(r"\d", stripped):
        problems.append(f"unapproved number: {re.findall(r'[^ ]*\d[^ ]*', stripped)[:3]}")
    if re.search(r"[\U0001F300-\U0001FAFF]", text):
        problems.append("emoji")
    if len(text.split()) >= 300:
        problems.append(f"{len(text.split())} words")
    return problems


LANE_OPENERS = {"AC": "", "A": "", "C": "", "D": "", **(settings.get("lane_openers", {}) or {})}
CANDIDATE = settings.get("candidate", {}) or {}
CANDIDATE_NAME = CANDIDATE.get("name", "<YOUR NAME>")


def clean_title(title: str) -> str:
    """'Head of Paid Media | Remote | LATAM Only | 82012' → 'Head of Paid Media'."""
    t = title.split("|")[0]
    t = re.sub(r"\s*\((?:[^)]*(?:remote|latam|global|only|brazil|part-time|m/w/d)[^)]*)\)", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -–")


def template_letter(job: Job, proofs: list[tuple], gap: tuple[str, str]) -> str:
    title = clean_title(job.title)
    role = f"the {title} role at {job.company}" if job.company else f"the {title} role"
    lines = [
        f"Hi {job.company or 'there'} team,",
        "",
        f"I'm applying for {role}. {LANE_OPENERS.get(job.lane, LANE_OPENERS['A'])}",
        "",
    ]
    for p in proofs[:2]:  # config may define fewer than two proofs
        lines += [p[5], ""]
    note = CANDIDATE.get("industry_note") or {}
    if note.get("text") and job.industry == note.get("industry"):
        lines += [note["text"], ""]
    # Name the gap unless it is already covered elsewhere in the letter.
    redundant = {"Nothing explicit in the posting"} | ({"Recent DTC depth"} if any(p[0] == "dtc" for p in proofs) else set())
    if gap[0] not in redundant:
        lines += [f"To be straight about one gap: {gap[1] if gap[1][:2] in ('I ', "I'") else gap[1][0].lower() + gap[1][1:]}", ""]
    lines += [
        CANDIDATE.get("location_line", ""),
        "",
        "I would like to talk about what you need next.",
        "",
        CANDIDATE_NAME,
    ]
    return "\n".join(lines)


def claude_letter(job: Job, proofs, gap, fallback: str) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback
    try:
        import anthropic
        profile = settings.read_optional(CANDIDATE.get("profile_file"))
        rules = settings.read_optional(CANDIDATE.get("rules_file"))
        if not profile:
            return fallback  # no facts file → never let the model invent a profile
        prompt = (
            f"Write a cover letter for {CANDIDATE_NAME}.\nRULES:\n{rules}\nPROFILE (the only source of facts):\n{profile}\n"
            f"JOB: {job.title} at {job.company}\n{job.description[:3500]}\n\n"
            f"Lead with: {[p[4] for p in proofs]}\nGap to address honestly: {gap[1]}\n\n"
            "Under 260 words, first person, short sentences. No 'passionate', 'proven track record', "
            "'data-driven', 'results-oriented'. No emoji. No numeric results (no %, ROAS, CPA, CPL values, uplift, revenue); "
            "only the allowed scope numbers. Sign as '{CANDIDATE_NAME}'. Output only the letter."
        )
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("JOBHUNT_MODEL", settings.get("llm.model", "claude-sonnet-5-5")), max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        problems = letter_ok(text, job)
        if problems:
            print(f"  Claude letter rejected ({problems}); using template.", file=sys.stderr)
            return fallback
        return text
    except Exception as e:
        print(f"  Claude letter failed ({e}); using template.", file=sys.stderr)
        return fallback


# ---------------------------------------------------------------- main


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_apps() -> dict:
    try:
        return json.loads(APPS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


CONTRACT = re.compile(r"\bcontract(?:or)?\b|freelance|\bpj\b|part[- ]time|meio per[íi]odo|\bhourly\b", re.I)


def kind_of(job: Job) -> str:
    k = (job.kind or "").lower()
    if k:
        return "Part-time" if "part" in k else "Contract" if re.search(r"contract|freelance|temp", k) else "Full-time"
    m = CONTRACT.search(f"{job.title}\n{job.description[:3000]}")
    if not m:
        return "Full-time"
    return "Part-time" if re.search(r"part|meio", m.group(0), re.I) else "Contract"


def write_sonar(jobs: list[Job], stats: dict, today: str) -> None:
    """Every job that passed the filters, with first-seen dates, for the sonar page (jobs/sonar.py)."""
    try:
        old = {j["key"]: j for j in json.loads(SONAR_FILE.read_text()).get("jobs", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}
    seen = load_seen()  # jobs from earlier reports keep their original date
    rows = []
    for j in jobs:
        brl = [round(brl_month(v)) for v in j.salary_usd_year] if j.salary_usd_year else None
        rows.append({
            "key": j.key, "title": j.title, "company": j.company, "url": j.url, "source": j.source,
            "location": j.location, "lane": j.lane, "lane_name": LANES[j.lane], "industry": j.industry,
            "kind": kind_of(j), "score": round(j.score), "breakdown": j.breakdown, "edges": j.edges, "flags": j.flags,
            "pay": fmt_pay(j), "pay_brl_month": brl,
            "published": j.published.isoformat() if j.published else None,
            "first_seen": old.get(j.key, {}).get("first_seen") or seen.get(j.key, today),
            "lead_with": [p[4] for p in lead_with(j)], "gap": list(gap_for(j)),
            "publicidade": is_pub(j), "agency": is_agency(j), "br": is_br(j),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    SONAR_FILE.write_text(json.dumps({"updated": NOW.isoformat(timespec="seconds"), "stats": stats, "jobs": rows},
                                     indent=1, ensure_ascii=False))


def brl_month(usd_year: float) -> float:
    return usd_year / 12 / USD_PER_BRL


def fmt_pay(job: Job) -> str:
    if job.salary_usd_year:
        lo, hi = (brl_month(v) for v in job.salary_usd_year)
        if hi < pay_floor(job):
            verdict = "below floor"
        elif hi < PAY_FLOOR_BRL_MONTH:
            verdict = "agency floor"
        elif lo >= PAY_TARGET_BRL_MONTH:
            verdict = "above target"
        else:
            verdict = "in range"
        return f"{job.salary_text or 'posted'} (≈{CUR_SYM}{lo/1000:.1f}K–{hi/1000:.1f}K/month, {verdict})"
    return "not posted"


def age(job: Job) -> str:
    if not job.published:
        return "unknown"
    h = (NOW - job.published).total_seconds() / 3600
    return f"{h:.0f}h ago" if h < 72 else f"{h/24:.0f}d ago"


def render(jobs: list[Job], stats: dict, today: str) -> str:
    out = [f"# Sniper jobs — {today}", "",
           f"Fetched {stats['fetched']} · after location/hard-drop {stats['kept']} · lane-fit {stats['laned']} · "
           f"new {stats['new']}. Sources: {', '.join(f'{k} ({v})' for k, v in stats['sources'].items())}.",
           "Manual checks (no allowed feed): niche boards and LinkedIn Jobs — see README.", ""]
    if not jobs:
        out.append("_No new jobs passed the filters today._")
    for i, j in enumerate(jobs, 1):
        b = j.breakdown
        proofs = lead_with(j)
        gap = gap_for(j)
        out += [
            f"## {i}. {j.title} — {j.company or '?'}  ·  **{j.score:.0f}/100**",
            f"- **Lane:** {j.lane}. {LANES[j.lane]} · **Industry:** {j.industry}"
            + (" (worked in)" if "Industry I've worked in" in j.edges else ""),
            f"- **Pay:** {fmt_pay(j)}",
            f"- **Location:** {j.location or 'n/a'} · **Posted:** {age(j)} · **Source:** {j.source}",
            f"- **Link:** {j.url}",
            f"- **Score:** lane {b['lane']} + edge {b['edge']} + pay {b['pay']} + fresh {b['fresh']:g} + seniority {b['senior']}"
            f" {b['penalty']:+d} penalties",
            f"- **Edges:** {', '.join(j.edges) or 'none'}" + (f" · **Penalties:** {', '.join(j.flags)}" if j.flags else ""),
            "- **Lead with:**",
            *[f"  - {p[4]}" for p in proofs],
            f"- **Gap to address:** {gap[0]}. *{gap[1]}*",
            "",
        ]
        if i <= 3:
            fallback = template_letter(j, proofs, gap)
            letter = claude_letter(j, proofs, gap, fallback)
            problems = letter_ok(letter, j)
            out += ["<details><summary>Draft application</summary>", "", letter, "",
                    f"_{len(letter.split())} words" + (f" · CHECK: {problems}" if problems else " · rules check passed") + "_",
                    "", "</details>", ""]
    return "\n".join(out)


def is_pub(job: Job) -> bool:
    """Advertising track: any agency job, plus every paid-media (A/AC) role."""
    return is_agency(job) or job.lane in ("A", "AC")


def render_pub(jobs: list[Job], today: str, top: int = 30) -> str:
    """out/agency/YYYY-MM-DD.md: Brazilian / Portuguese first, then English, each by score."""
    pub = sorted((j for j in jobs if is_pub(j)), key=lambda j: (is_br(j), j.score), reverse=True)[:top]
    out = [f"# Publicidade — {today}", "",
           f"{len(pub)} vagas (agência + mídia paga). Brasil/português primeiro, depois inglês. "
           "Busca manual (sem feed permitido): Gupy, LinkedIn Jobs, Vagas.com, Indeed BR.", ""]
    section = None
    for j in pub:
        s = "🇧🇷 Brasil / português" if is_br(j) else "🌎 Inglês / internacional"
        if s != section:
            out += [f"## {s}", ""]
            section = s
        proofs = lead_with(j)
        gap = gap_for(j)
        out += [f"### {j.title} — {j.company or '?'} · **{j.score:.0f}/100**" + (" · agência" if is_agency(j) else ""),
                f"- {j.url}",
                f"- Lane {j.lane} · {j.industry} · {kind_of(j)} · {fmt_pay(j)} · {age(j)} · {j.source}",
                f"- Abrir com: {proofs[0][4] if proofs else '—'}",
                f"- Lacuna: {gap[0]}", ""]
    if not pub:
        out.append("_Nenhuma vaga de publicidade passou nos filtros hoje._")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobs", description="Sniper job finder (never applies for you).")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="don't update data/seen.json")
    ap.add_argument("--debug", action="store_true", help="print why jobs were dropped")
    args = ap.parse_args(argv)

    DATA.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    today = NOW.astimezone().strftime("%Y-%m-%d")

    all_jobs, stats = [], {"sources": {}}
    from watchlist import fetch_watchlist
    sources = [("Remotive", fetch_remotive), ("Himalayas", fetch_himalayas), ("We Work Remotely", fetch_wwr),
               ("Get on Board", fetch_getonboard), ("Remote OK", fetch_remoteok), ("Jobicy", fetch_jobicy),
               ("Working Nomads", fetch_workingnomads), ("Remotar", fetch_remotar), ("Company pages", fetch_watchlist)]
    for name, fn in sources:
        try:
            got = fn()
            print(f"{name}: {len(got)} jobs")
        except Exception as e:
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            got = []
        stats["sources"][name] = len(got)
        all_jobs += got
    stats["fetched"] = len(all_jobs)

    dedup, loc_fail = {}, {}
    for j in all_jobs:
        ok, why = location_ok(j)
        if not ok:  # if any source restricts location, the job is restricted
            loc_fail.setdefault(j.key, why)
        prev = dedup.get(j.key)
        if not prev or (j.salary_usd_year and not prev.salary_usd_year):
            dedup[j.key] = j

    kept, dropped = [], []
    for j in dedup.values():
        ok, why = (False, loc_fail[j.key]) if j.key in loc_fail else (True, "")
        why = why or hard_drop(j)
        if not ok or why:
            dropped.append((j, why))
            continue
        kept.append(j)
    stats["kept"] = len(kept)

    laned = []
    for j in kept:
        j.lane, pts = assign_lane(j)
        if not j.lane:
            dropped.append((j, "no lane fit"))
            continue
        score(j, pts)
        laned.append(j)
    stats["laned"] = len(laned)

    write_sonar(laned, stats, today)  # all of them; the page shows saved/applied/hidden status
    apps = load_apps()
    laned = [j for j in laned if apps.get(j.key, {}).get("status") not in ("applied", "hidden")]

    seen = load_seen()
    fresh = [j for j in laned if j.key not in seen or seen[j.key] == today]
    stats["new"] = len(fresh)
    fresh.sort(key=lambda j: (j.score, j.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    top = fresh[: args.top]

    if args.debug:
        for j, why in dropped:
            print(f"  DROP [{j.source}] {j.title} — {j.company}: {why}")

    PUB_DIR.mkdir(parents=True, exist_ok=True)
    (PUB_DIR / f"{today}.md").write_text(render_pub(laned, today))

    path = OUT / f"{today}.md"
    path.write_text(render(top, stats, today))
    if not args.dry_run:
        for j in top:
            seen.setdefault(j.key, today)
        SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True))
    print(f"\nWrote {path.relative_to(ROOT)} — {len(top)} jobs. Top: " +
          ", ".join(f"{j.score:.0f} {j.title} ({j.company})" for j in top[:3]))
    return path


if __name__ == "__main__":
    main()
