"""The agent's loop: discover -> triage (score + draft) -> submit, with you in the middle."""

from __future__ import annotations

import json
from pathlib import Path

from job_agent import browser
from job_agent.config import Config, Profile
from job_agent.db import Tracker
from job_agent.llm import Claude, LLMError
from job_agent.materials import cover_letter_pdf, load_tailored, write_materials
from job_agent.sources import Job, fetch_all, prefilter


def discover(cfg: Config, tracker: Tracker) -> int:
    jobs = fetch_all(cfg.sources)
    added = sum(tracker.add_job(j) for j in jobs)
    print(f"Fetched {len(jobs)} open roles, {added} new.")
    return added


def decide(assessment, cfg: Config) -> str:
    """Route a scored job. This is the only place auto-submission is granted."""
    p = cfg.policy
    if assessment.dealbreakers_hit or assessment.recommendation == "skip" or assessment.score < p.review_threshold:
        return "skipped"
    if p.auto_submit and assessment.recommendation == "apply" and assessment.score >= p.auto_submit_threshold:
        return "auto_approved"
    return "pending_review"


def triage(cfg: Config, profile: Profile, tracker: Tracker, claude: Claude, pdf: bool = True) -> None:
    jobs = tracker.by_status("new")
    scored = 0
    for job in jobs:
        reason = prefilter(_as_job(job), profile.preferences)
        if reason:
            tracker.update(job["key"], "prefilter", status="skipped", note=reason)
            continue
        if scored >= cfg.policy.max_jobs_scored_per_run:
            print(f"Reached max_jobs_scored_per_run ({scored}); the rest wait for the next run.")
            break
        scored += 1
        try:
            assessment = claude.assess(job)
            status = decide(assessment, cfg)
            tracker.update(job["key"], "assessed", score=assessment.score,
                           assessment=assessment.model_dump(), note=assessment.summary)
            if status != "skipped":
                tailored = claude.tailor(job)
                out = write_materials(cfg.data_dir, profile, job, tailored, pdf=pdf)
                tracker.update(job["key"], materials_dir=str(out))
            tracker.update(job["key"], "triaged", status=status)
        except LLMError as e:
            tracker.log(job["key"], "error", str(e))
            print(f"  ! {job['company']} / {job['title']}: {e}")
            continue
        print(f"  {assessment.score:>3}  {status:<15} {job['company']} / {job['title']}")


def submit(
    cfg: Config,
    profile: Profile,
    tracker: Tracker,
    claude: Claude,
    keys: list[str] | None = None,
    *,
    headed: bool = False,
    dry_run: bool = False,
) -> None:
    rows = [tracker.find(k) for k in keys] if keys else tracker.by_status("approved", "auto_approved")
    if not rows:
        print("Nothing approved to submit.")
        return
    for job in rows:
        if job["status"] not in ("approved", "auto_approved", "needs_manual", "failed"):
            print(f"  - skipping {job['key']} (status {job['status']}; approve it first)")
            continue
        if not dry_run and tracker.submitted_today() >= cfg.policy.max_applications_per_day:
            print(f"Daily cap of {cfg.policy.max_applications_per_day} reached; stopping.")
            return
        materials = Path(job["materials_dir"])
        tailored = load_tailored(materials)
        files = {"RESUME": materials / "resume.pdf", "COVER_LETTER": cover_letter_pdf(materials, profile)}
        print(f"→ {job['company']} / {job['title']}\n  {job['apply_url']}")
        try:
            outcome = browser.apply(
                job["apply_url"],
                lambda fields: claude.answer_form(job, tailored, fields),
                files,
                materials,
                headed=headed,
                submit=not dry_run,
                confirm_fn=_ask_human if headed else None,
            )
        except LLMError as e:
            outcome = browser.ApplyOutcome("failed", str(e))
        print(f"  {outcome.status}: {outcome.detail}")
        if outcome.fill and outcome.fill.open_questions and not headed:
            print("  Finish it with: python -m job_agent apply --headed " + job["key"])
        if outcome.status == "dry_run":
            print(f"  Screenshot: {materials / 'filled.png'}")
            continue
        answers = outcome.fill.answers if outcome.fill else None
        tracker.update(job["key"], "apply", status=outcome.status, note=outcome.detail,
                       **({"answers": answers} if answers else {}))


def _ask_human(result: browser.FillResult) -> tuple[str, str]:
    print("\n  The form is filled in the browser window.")
    for q in result.open_questions:
        print(f"  ? {q['label'][:100]}\n      suggested: {q['suggestion'] or '(blank)'}  — {q['note']}")
    for e in result.errors:
        print(f"  ! could not fill {e}")
    print("  Check everything, fix anything above, and click submit yourself.")
    choice = input("  Did you submit it? [y]es / [n]o, leave for later: ").strip().lower()
    if choice.startswith("y"):
        return "submitted", "submitted by you in the browser"
    return "needs_manual", "left for later"


def review(cfg: Config, tracker: Tracker) -> None:
    jobs = tracker.by_status("pending_review")
    if not jobs:
        print("Review queue is empty.")
        return
    print(f"{len(jobs)} application(s) waiting for you.\n")
    for i, job in enumerate(jobs, 1):
        a = json.loads(job["assessment"] or "{}")
        print("=" * 72)
        print(f"[{i}/{len(jobs)}] {job['company']} — {job['title']}  (score {job['score']})")
        print(f"Location: {job['location'] or '-'}   Pay: {job['compensation'] or 'not listed'}")
        print(f"Posting:  {job['url']}")
        print(f"\n{a.get('summary', '')}")
        for s in a.get("strengths", []):
            print(f"  + {s}")
        for g in a.get("gaps", []):
            print(f"  - {g}")
        print(f"\nDrafts: {job['materials_dir']}/ (resume.pdf, cover_letter.txt — edit freely)")
        while True:
            choice = input("[a]pprove  [r]eject  [c]over letter  [s]kip  [q]uit > ").strip().lower()
            if choice == "c":
                print("\n" + (Path(job["materials_dir"]) / "cover_letter.txt").read_text())
                continue
            break
        if choice == "a":
            tracker.update(job["key"], "review", status="approved")
        elif choice == "r":
            tracker.update(job["key"], "review", status="rejected")
        elif choice == "q":
            break
    print("\nApproved roles are submitted by `python -m job_agent apply` (or the next `run`).")


def _as_job(row) -> Job:
    return Job(**{k: row[k] for k in ("source", "company", "job_id", "title", "location", "url",
                                       "apply_url", "description", "posted_at", "compensation")})
