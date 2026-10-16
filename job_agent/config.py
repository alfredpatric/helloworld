"""Profile (who you are) and config (how the agent behaves), loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Experience(BaseModel):
    company: str
    title: str
    start: str
    end: str = "Present"
    location: str = ""
    bullets: list[str]


class Education(BaseModel):
    school: str
    degree: str
    year: str = ""


class Person(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Preferences(BaseModel):
    target_titles: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    min_salary: int | None = None
    dealbreakers: list[str] = Field(default_factory=list)
    companies_to_avoid: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    person: Person
    headline: str = ""
    summary: str
    skills: list[str]
    experience: list[Experience]
    education: list[Education] = Field(default_factory=list)
    # Standard answers: work authorization, sponsorship, salary, notice period, etc.
    # The agent uses these verbatim and never guesses legal or factual answers.
    standard_answers: dict[str, str] = Field(default_factory=dict)
    # Voluntary self-identification answers; anything missing is declined.
    eeo: dict[str, str] = Field(default_factory=dict)
    preferences: Preferences = Field(default_factory=Preferences)


class Sources(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    # Score >= auto_submit_threshold with no open questions -> submitted without review.
    auto_submit: bool = False
    auto_submit_threshold: int = 85
    # Score >= review_threshold -> drafted and queued for your approval.
    review_threshold: int = 65
    max_applications_per_day: int = 10
    max_jobs_scored_per_run: int = 50


class ModelConfig(BaseModel):
    name: str = "claude-opus-5-5"
    scoring_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    writing_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


class Config(BaseModel):
    sources: Sources = Field(default_factory=Sources)
    policy: Policy = Field(default_factory=Policy)
    model: ModelConfig = Field(default_factory=ModelConfig)
    data_dir: Path = Path("data")


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run `python -m job_agent init` first.")
    return yaml.safe_load(path.read_text()) or {}


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(_load_yaml(path))


def load_config(path: Path) -> Config:
    return Config.model_validate(_load_yaml(path))
