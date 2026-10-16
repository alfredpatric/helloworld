"""Command line: python -m job_agent <command>."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from job_agent import pipeline
from job_agent.config import load_config, load_profile
from job_agent.db import STATUSES, Tracker

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="job_agent", description="Semi-automatic job application agent")
    ap.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    ap.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create profile.yaml and config.yaml from the examples")
    sub.add_parser("discover", help="fetch new roles from your job boards")
    t = sub.add_parser("triage", help="score new roles and draft materials for good fits")
    t.add_argument("--no-pdf", action="store_true", help="skip PDF rendering (HTML only)")
    sub.add_parser("review", help="approve or reject drafted applications")
    a = sub.add_parser("apply", help="submit approved applications")
    a.add_argument("keys", nargs="*", help="specific job keys (default: all approved)")
    a.add_argument("--headed", action="store_true", help="show the browser; you click submit")
    a.add_argument("--dry-run", action="store_true", help="fill forms and screenshot, never submit")
    r = sub.add_parser("run", help="discover + triage + submit approved (for a daily schedule)")
    r.add_argument("--no-submit", action="store_true", help="stop after drafting")
    s = sub.add_parser("status", help="show counts and the active pipeline")
    s.add_argument("--all", action="store_true", help="include skipped and closed roles")
    m = sub.add_parser("mark", help="set a job's status by hand (e.g. interviewing)")
    m.add_argument("key")
    m.add_argument("status", choices=STATUSES)
    m.add_argument("--note", default="")

    args = ap.parse_args(argv)
    if args.cmd == "init":
        return _init(args)

    cfg = load_config(args.config)
    tracker = Tracker(cfg.data_dir / "jobs.db")

    if args.cmd == "status":
        _status(tracker, args.all)
        return 0
    if args.cmd == "mark":
        job = tracker.find(args.key)
        tracker.update(job["key"], "manual", status=args.status, note=args.note or args.status)
        print(f"{job['company']} / {job['title']} -> {args.status}")
        return 0
    if args.cmd == "review":
        pipeline.review(cfg, tracker)
        return 0
    if args.cmd == "discover":
        pipeline.discover(cfg, tracker)
        return 0

    from job_agent.llm import Claude  # only needed (and needs credentials) from here on

    profile = load_profile(args.profile)
    claude = Claude(profile, cfg.model)
    if args.cmd == "triage":
        pipeline.triage(cfg, profile, tracker, claude, pdf=not args.no_pdf)
    elif args.cmd == "apply":
        pipeline.submit(cfg, profile, tracker, claude, args.keys, headed=args.headed, dry_run=args.dry_run)
    elif args.cmd == "run":
        pipeline.discover(cfg, tracker)
        pipeline.triage(cfg, profile, tracker, claude)
        if not args.no_submit:
            pipeline.submit(cfg, profile, tracker, claude)
        _status(tracker, False)
    return 0


def _init(args) -> int:
    for target, example in ((args.profile, "profile.example.yaml"), (args.config, "config.example.yaml")):
        if target.exists():
            print(f"{target} already exists; leaving it alone.")
        else:
            shutil.copy(ROOT / example, target)
            print(f"Created {target} — fill it in.")
    return 0


def _status(tracker: Tracker, show_all: bool) -> None:
    counts = tracker.counts()
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet.")
    active = ("pending_review", "approved", "auto_approved", "needs_manual", "failed", "submitted",
              "interviewing", "offer")
    rows = tracker.by_status(*(STATUSES if show_all else active))
    for r in rows:
        print(f"  {r['status']:<15} {r['score'] if r['score'] is not None else '-':>3}  "
              f"{r['company']} / {r['title']}  [{r['key']}]")
        if r["status"] in ("needs_manual", "failed") and r["note"]:
            print(f"      {r['note'][:150]}")
