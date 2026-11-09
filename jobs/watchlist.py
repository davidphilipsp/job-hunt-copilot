"""Company watchlist: read open roles straight from companies' public ATS job feeds.

Greenhouse, Lever and Ashby publish every company's job board as a public JSON API meant for this.
Companies live in data/watchlist.json as {"name", "ats", "slug"}; add or remove them freely.
Their feeds list office jobs too, so these jobs must say "remote" in the title or location (finder.location_ok).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from finder import DATA, MARKETING_TITLE, NOT_CORE, Job, get, parse_iso, strip_html

WATCHLIST_FILE = DATA / "watchlist.json"


def load_watchlist() -> list[dict]:
    try:
        return json.loads(WATCHLIST_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def relevant(title: str) -> bool:
    """Skip engineering/sales roles before reading their full description."""
    return bool(MARKETING_TITLE.search(title)) and not NOT_CORE.search(title)


def greenhouse(c: dict) -> list[Job]:
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs", params={"content": "true"})
    return [Job(
        source=f"Company page ({c['name']})", title=j["title"], company=c["name"], url=j["absolute_url"],
        location=(j.get("location") or {}).get("name", ""), description=strip_html(j.get("content", "")),
        published=parse_iso(j.get("first_published") or j.get("updated_at")), needs_remote_proof=True,
    ) for j in r.json().get("jobs", []) if relevant(j["title"])]


def lever(c: dict) -> list[Job]:
    r = get(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"})
    jobs = []
    for j in r.json():
        if not relevant(j["text"]):
            continue
        cat = j.get("categories") or {}
        loc = ", ".join(cat.get("allLocations") or [cat.get("location") or ""])
        if (j.get("workplaceType") or "") == "remote" and "remote" not in loc.lower():
            loc = f"Remote, {loc}".strip(", ")
        desc = "\n".join([j.get("descriptionPlain") or ""] +
                         [f"{l.get('text', '')}\n{strip_html(l.get('content', ''))}" for l in j.get("lists") or []])
        jobs.append(Job(
            source=f"Company page ({c['name']})", title=j["text"], company=c["name"], url=j["hostedUrl"],
            location=loc, description=desc, needs_remote_proof=True, kind=cat.get("commitment") or "",
            published=datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc) if j.get("createdAt") else None,
        ))
    return jobs


def ashby(c: dict) -> list[Job]:
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}", params={"includeCompensation": "true"})
    jobs = []
    for j in r.json().get("jobs", []):
        if not relevant(j["title"]) or j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = ", ".join(l for l in locs if l)
        if (j.get("isRemote") or j.get("workplaceType") == "Remote") and "remote" not in loc.lower():
            loc = f"Remote, {loc}".strip(", ")
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        jobs.append(Job(
            source=f"Company page ({c['name']})", title=j["title"], company=c["name"], url=j["jobUrl"],
            location=loc, description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            published=parse_iso(j.get("publishedAt")), salary_text=comp, needs_remote_proof=True,
            kind=j.get("employmentType") or "",
        ))
    return jobs


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def fetch_watchlist() -> list[Job]:
    from finder import parse_salary_text

    jobs = []
    for c in load_watchlist():
        fn = ATS.get(c.get("ats", ""))
        if not fn:
            print(f"  watchlist: unknown ATS for {c}", file=sys.stderr)
            continue
        try:
            got = fn(c)
        except Exception as e:
            print(f"  watchlist {c['name']} failed: {e}", file=sys.stderr)
            continue
        for j in got:
            j.salary_usd_year = parse_salary_text(j.salary_text) if j.salary_text else None
        jobs += got
    return jobs
