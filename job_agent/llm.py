"""Claude calls: assess fit, tailor materials, and answer application forms.

Every call returns validated structured output. Your profile goes in a cached
system prompt, so repeated calls in one run only pay for it once.
"""

from __future__ import annotations

import json
from typing import Literal

import anthropic
from pydantic import BaseModel

from job_agent.config import ModelConfig, Profile
from job_agent.sources import Job

SYSTEM = """You are a careful job-application assistant acting on behalf of the candidate below.

Hard rules:
- Never invent experience, employers, titles, dates, degrees, certifications, skills, metrics or
  personal facts. You may only select, reorder, condense and rephrase what is in the profile.
- Legal and factual questions (work authorization, sponsorship, salary, start date, relocation,
  criminal history, etc.) must be answered from `standard_answers` exactly. If the profile does
  not contain the answer, say you are not confident so a human can answer it.
- Voluntary self-identification (gender, race, veteran, disability) uses `eeo` answers when
  given, otherwise the option meaning "decline to self-identify".
- Write plainly and specifically. No clichés, no exaggeration, no em-dash-heavy prose.

Candidate profile (JSON):
{profile}
"""


class Assessment(BaseModel):
    score: int  # 0-100 overall fit
    recommendation: Literal["apply", "review", "skip"]
    dealbreakers_hit: list[str]
    strengths: list[str]
    gaps: list[str]
    summary: str


class TailoredExperience(BaseModel):
    index: int  # position in the profile's experience list
    bullets: list[str]


class Tailored(BaseModel):
    summary: str
    skills: list[str]
    experiences: list[TailoredExperience]
    cover_letter: str


class FieldAnswer(BaseModel):
    field_id: str
    value: str
    confident: bool
    note: str


class FormAnswers(BaseModel):
    answers: list[FieldAnswer]


class LLMError(RuntimeError):
    pass


class Claude:
    def __init__(self, profile: Profile, model: ModelConfig, client: anthropic.Anthropic | None = None):
        self.profile = profile
        self.model = model
        self.client = client or anthropic.Anthropic()
        self.system = [
            {
                "type": "text",
                "text": SYSTEM.format(profile=profile.model_dump_json(indent=1)),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _ask(self, prompt: str, schema: type[BaseModel], effort: str):
        try:
            response = self.client.beta.messages.parse(
                model=self.model.name,
                max_tokens=16000,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                output_format=schema,
                # If a safety classifier declines, retry server-side on Anthropic's recommended model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Claude API: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise LLMError(f"Incomplete response (stop_reason={response.stop_reason})")
        return response.parsed_output

    @staticmethod
    def _job_block(job) -> str:
        return (
            f"<job>\nCompany: {job['company']}\nTitle: {job['title']}\nLocation: {job['location']}\n"
            f"Compensation: {job['compensation'] or 'not listed'}\n\n{job['description']}\n</job>"
        )

    def assess(self, job) -> Assessment:
        prefs = self.profile.preferences
        result = self._ask(
            f"""{self._job_block(job)}

Assess how well this role fits the candidate, from the hiring manager's view (can they do the
job, is the seniority right) and the candidate's view (preferences and dealbreakers below).

Preferences: {prefs.model_dump_json()}

Score 0-100: 85+ strong fit worth applying immediately, 65-84 plausible, below 65 poor.
Any dealbreaker hit means recommendation "skip" and a score below 50. List dealbreakers_hit only
for dealbreakers clearly present in the posting.""",
            Assessment,
            self.model.scoring_effort,
        )
        result.score = max(0, min(100, result.score))
        return result

    def tailor(self, job) -> Tailored:
        n = len(self.profile.experience)
        result = self._ask(
            f"""{self._job_block(job)}

Tailor the candidate's resume and write a cover letter for this role.

- summary: 2-3 sentences positioning the candidate for this role.
- skills: up to 15 skills from the profile, most relevant first. Only skills in the profile.
- experiences: one entry per profile experience you include, `index` 0-{n - 1} in profile order.
  Choose and rephrase that role's bullets to emphasise what this job asks for; keep every claim
  and number faithful to the original bullet. 2-5 bullets each. Include the most recent role.
- cover_letter: under 300 words, addressed "Dear Hiring Team,", signed with the candidate's name.
  Name two or three specific reasons the candidate fits, tied to real profile facts.""",
            Tailored,
            self.model.writing_effort,
        )
        result.experiences = [e for e in result.experiences if 0 <= e.index < n]
        if not result.experiences:
            raise LLMError("Tailored resume had no valid experience entries")
        return result

    def answer_form(self, job, tailored: Tailored, fields: list[dict]) -> FormAnswers:
        return self._ask(
            f"""{self._job_block(job)}

<cover_letter>
{tailored.cover_letter}
</cover_letter>

Fill in this application form. Fields (JSON):
{json.dumps(fields, indent=1)}

Return one answer per field_id.
- For file fields, value is "RESUME", "COVER_LETTER" or "" (leave empty).
- For select/radio/combobox fields, value must be exactly one of the listed options.
- For checkbox groups, value is the chosen option labels joined with " | ".
- For free-text questions ("Why do you want to work here?") write 2-4 honest sentences grounded
  in the job post and the profile.
- confident=false whenever the answer is not directly supported by the profile, the question is
  legal or factual and missing from standard_answers, or you would otherwise be guessing. Put
  your best suggestion in value and explain in note. Leave optional fields you cannot answer
  as "" with confident=true.""",
            FormAnswers,
            self.model.writing_effort,
        )
