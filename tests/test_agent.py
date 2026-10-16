import json
from pathlib import Path

import pytest
import yaml

from job_agent import browser, pipeline
from job_agent.config import Config, Profile
from job_agent.db import Tracker
from job_agent.llm import Assessment, FieldAnswer, FormAnswers, Tailored, TailoredExperience
from job_agent.materials import render_resume_html
from job_agent.sources import parse_ashby, parse_greenhouse, parse_lever, prefilter

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(yaml.safe_load((ROOT / "profile.example.yaml").read_text()))


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config.model_validate(yaml.safe_load((ROOT / "config.example.yaml").read_text()))
    c.data_dir = tmp_path
    return c


def load(name):
    return json.loads((FIX / name).read_text())


def test_parsers():
    gh = parse_greenhouse("acme", load("greenhouse.json"))
    assert gh[0].key == "greenhouse:acme:4012345"
    assert "Build the ledger." in gh[0].description and "- Go" in gh[0].description
    lv = parse_lever("beta", load("lever.json"))
    assert lv[0].apply_url.endswith("/apply") and "Terraform" in lv[0].description
    assert "180000" in lv[0].compensation
    ab = parse_ashby("gamma", load("ashby.json"))
    assert len(ab) == 1 and "(Remote)" in ab[0].location and ab[0].compensation == "$170K – $210K"


def test_prefilter(profile):
    gh = parse_greenhouse("acme", load("greenhouse.json"))
    assert prefilter(gh[0], profile.preferences) is None
    assert prefilter(gh[1], profile.preferences) == "title matches exclude list"
    assert prefilter(parse_lever("beta", load("lever.json"))[0], profile.preferences) is None


def assessment(score, rec="apply", dealbreakers=()):
    return Assessment(score=score, recommendation=rec, dealbreakers_hit=list(dealbreakers),
                      strengths=["Go"], gaps=[], summary="ok")


def test_decide(cfg):
    assert pipeline.decide(assessment(90), cfg) == "pending_review"  # auto_submit off by default
    cfg.policy.auto_submit = True
    assert pipeline.decide(assessment(90), cfg) == "auto_approved"
    assert pipeline.decide(assessment(90, "review"), cfg) == "pending_review"
    assert pipeline.decide(assessment(70), cfg) == "pending_review"
    assert pipeline.decide(assessment(50), cfg) == "skipped"
    assert pipeline.decide(assessment(95, dealbreakers=["clearance"]), cfg) == "skipped"


def test_tracker_dedup_and_status(tmp_path):
    t = Tracker(tmp_path / "jobs.db")
    job = parse_greenhouse("acme", load("greenhouse.json"))[0]
    assert t.add_job(job) and not t.add_job(job)
    t.update(job.key, "x", status="submitted")
    assert t.submitted_today() == 1 and t.counts() == {"submitted": 1}
    with pytest.raises(ValueError):
        t.update(job.key, status="bogus")


def test_resume_uses_profile_facts(profile):
    tailored = Tailored(summary="S", skills=["Go"], cover_letter="Hi",
                        experiences=[TailoredExperience(index=1, bullets=["Built <pipelines>"])])
    html = render_resume_html(profile, tailored)
    assert "DataNest" in html and "Jun 2018" in html and "&lt;pipelines&gt;" in html
    assert "Paystream" not in html


class FakeClaude:
    def assess(self, job):
        return assessment(90 if "Payments" in job["title"] else 40)

    def tailor(self, job):
        return Tailored(summary="S", skills=["Go"], cover_letter="Dear Hiring Team,\n\nHi.",
                        experiences=[TailoredExperience(index=0, bullets=["b"])])

    def answer_form(self, job, tailored, fields):
        by_label = {
            "First Name": "Alex", "Last Name": "Example", "Email": "alex@example.com",
            "Resume/CV": "RESUME", "Are you legally authorized to work in the US?": "Yes",
            "Will you require visa sponsorship?": "No", "Why do you want to work here?": "The ledger work.",
        }
        return FormAnswers(answers=[
            FieldAnswer(field_id=f["field_id"], value=by_label.get(f["label"], ""), confident=True, note="")
            for f in fields
        ])


def test_triage_routes_and_drafts(cfg, profile, tmp_path):
    t = Tracker(tmp_path / "jobs.db")
    for j in parse_greenhouse("acme", load("greenhouse.json")) + parse_lever("beta", load("lever.json")):
        t.add_job(j)
    pipeline.triage(cfg, profile, t, FakeClaude(), pdf=False)
    assert t.get("greenhouse:acme:4012345")["status"] == "pending_review"
    assert t.get("greenhouse:acme:4012346")["status"] == "skipped"  # prefiltered (iOS)
    assert t.get("lever:beta:a1b2c3")["status"] == "skipped"  # scored 40
    drafted = Path(t.get("greenhouse:acme:4012345")["materials_dir"])
    assert (drafted / "cover_letter.txt").read_text().startswith("Dear Hiring Team")


def test_browser_scans_fills_and_submits(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    seen = {}

    def answer(fields):
        seen["fields"] = fields
        return FakeClaude().answer_form(None, None, fields)

    out = browser.apply((FIX / "form.html").as_uri(), answer, {"RESUME": resume}, tmp_path)
    labels = {f["label"]: f for f in seen["fields"]}
    assert labels["First Name"]["required"] is True
    assert labels["Resume/CV"]["type"] == "file"
    assert labels["Are you legally authorized to work in the US?"]["options"] == ["Yes", "No"]
    assert labels["Will you require visa sponsorship?"]["options"] == ["Yes", "No"]
    assert "Leave blank" not in labels  # hidden honeypot is ignored
    assert out.status == "submitted", out.detail
    assert (tmp_path / "after_submit.png").exists()


def test_browser_holds_back_when_unsure(tmp_path):
    def answer(fields):
        result = FakeClaude().answer_form(None, None, fields)
        for a in result.answers:
            if a.value == "No":
                a.confident, a.note = False, "sponsorship not in profile"
        return result

    out = browser.apply((FIX / "form.html").as_uri(), answer, {}, tmp_path)
    assert out.status == "needs_manual"
    assert "sponsorship" in out.detail and "Resume/CV" in out.detail  # required file not provided
    assert not (tmp_path / "after_submit.png").exists()


def test_dry_run_never_submits(tmp_path):
    out = browser.apply((FIX / "form.html").as_uri(), lambda f: FakeClaude().answer_form(None, None, f),
                        {}, tmp_path, submit=False)
    assert out.status == "dry_run" and (tmp_path / "filled.png").exists()
