"""Fetch open roles from the public job-board APIs of Greenhouse, Lever and Ashby.

These are the same JSON feeds that power companies' own career pages, so no
scraping or login is needed. Each company is identified by its board slug, e.g.
https://job-boards.greenhouse.io/<slug> or https://jobs.lever.co/<slug>.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from job_agent.config import Preferences, Sources


@dataclass
class Job:
    source: str
    company: str
    job_id: str
    title: str
    location: str
    url: str
    apply_url: str
    description: str
    posted_at: str = ""
    compensation: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.job_id}"


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "job-agent/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def html_to_text(raw: str) -> str:
    text = html.unescape(raw or "")  # Greenhouse double-escapes its HTML
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_greenhouse(company: str, data: dict) -> list[Job]:
    return [
        Job(
            source="greenhouse",
            company=company,
            job_id=str(j["id"]),
            title=j["title"],
            location=(j.get("location") or {}).get("name", ""),
            url=j["absolute_url"],
            apply_url=j["absolute_url"],
            description=html_to_text(j.get("content", "")),
            posted_at=j.get("updated_at", ""),
        )
        for j in data.get("jobs", [])
    ]


def parse_lever(company: str, data: list) -> list[Job]:
    jobs = []
    for j in data:
        sections = "\n\n".join(
            f"{s.get('text', '')}\n{html_to_text(s.get('content', ''))}" for s in j.get("lists", [])
        )
        description = "\n\n".join(
            p for p in (j.get("descriptionPlain", ""), sections, j.get("additionalPlain", "")) if p
        )
        cats = j.get("categories") or {}
        salary = j.get("salaryRange") or {}
        comp = ""
        if salary.get("min"):
            comp = f"{salary.get('currency', '')} {salary['min']}-{salary.get('max', '')} {salary.get('interval', '')}".strip()
        jobs.append(
            Job(
                source="lever",
                company=company,
                job_id=j["id"],
                title=j["text"],
                location=cats.get("location", "") or j.get("workplaceType", ""),
                url=j["hostedUrl"],
                apply_url=j.get("applyUrl") or j["hostedUrl"] + "/apply",
                description=description,
                posted_at=str(j.get("createdAt", "")),
                compensation=comp,
            )
        )
    return jobs


def parse_ashby(company: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        location = j.get("location", "")
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        jobs.append(
            Job(
                source="ashby",
                company=company,
                job_id=j["id"],
                title=j["title"],
                location=location,
                url=j["jobUrl"],
                apply_url=j.get("applyUrl") or j["jobUrl"] + "/application",
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                posted_at=j.get("publishedAt", ""),
                compensation=comp,
            )
        )
    return jobs


FEEDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", parse_ashby),
}


def fetch_all(sources: Sources) -> list[Job]:
    jobs: list[Job] = []
    for source, (url_template, parse) in FEEDS.items():
        for slug in getattr(sources, source):
            try:
                jobs.extend(parse(slug, _get_json(url_template.format(slug=slug))))
            except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
                print(f"  ! could not fetch {source}/{slug}: {e}", file=sys.stderr)
    return jobs


def prefilter(job: Job, prefs: Preferences) -> str | None:
    """Cheap keyword checks before spending a model call. Returns a skip reason or None."""
    title = job.title.lower()
    if any(c.lower() == job.company.lower() for c in prefs.companies_to_avoid):
        return "company on avoid list"
    if any(word.lower() in title for word in prefs.title_exclude):
        return "title matches exclude list"
    if prefs.target_titles and not any(t.lower() in title for t in prefs.target_titles):
        return "title not in target titles"
    if prefs.locations:
        loc = job.location.lower()
        remote = "remote" in loc or "anywhere" in loc
        if not (any(l.lower() in loc for l in prefs.locations) or (prefs.remote_ok and remote) or not loc):
            return f"location '{job.location}' not wanted"
    return None
