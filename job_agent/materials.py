"""Turn a tailored draft into files: resume (HTML + PDF), cover letter, and a JSON record."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from job_agent.config import Profile
from job_agent.llm import Tailored

CSS = """
body { font: 10.5pt/1.35 Georgia, 'Times New Roman', serif; color: #111; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2px; }
.contact { color: #444; margin-bottom: 10px; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: .06em;
     border-bottom: 1px solid #999; margin: 14px 0 6px; padding-bottom: 2px; }
.role { display: flex; justify-content: space-between; font-weight: bold; margin-top: 8px; }
.org { font-style: italic; }
ul { margin: 3px 0 0 18px; padding: 0; }
li { margin: 1px 0; }
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def render_resume_html(profile: Profile, tailored: Tailored) -> str:
    e = html.escape
    p = profile.person
    contact = " · ".join(x for x in (p.email, p.phone, p.location, p.linkedin, p.github, p.website) if x)
    roles = []
    for t in tailored.experiences:
        x = profile.experience[t.index]  # company/title/dates always come from the profile
        bullets = "".join(f"<li>{e(b)}</li>" for b in t.bullets)
        roles.append(
            f'<div class="role"><span>{e(x.title)}</span><span>{e(x.start)} – {e(x.end)}</span></div>'
            f'<div class="org">{e(x.company)}{", " + e(x.location) if x.location else ""}</div><ul>{bullets}</ul>'
        )
    education = "".join(
        f'<div class="role"><span>{e(ed.degree)}</span><span>{e(ed.year)}</span></div>'
        f'<div class="org">{e(ed.school)}</div>'
        for ed in profile.education
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(p.full_name)} – Resume</title>
<style>{CSS}</style></head><body>
<h1>{e(p.full_name)}</h1><div class="contact">{e(contact)}</div>
<h2>Summary</h2><p>{e(tailored.summary)}</p>
<h2>Experience</h2>{"".join(roles)}
{"<h2>Education</h2>" + education if education else ""}
<h2>Skills</h2><p>{e(", ".join(tailored.skills))}</p>
</body></html>"""


def html_to_pdf(html_text: str, out: Path) -> None:
    from playwright.sync_api import sync_playwright

    from job_agent.browser import launch

    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page()
        page.set_content(html_text)
        page.pdf(path=str(out), format="Letter",
                 margin={"top": "0.6in", "bottom": "0.6in", "left": "0.7in", "right": "0.7in"})
        browser.close()


def write_materials(data_dir: Path, profile: Profile, job, tailored: Tailored, pdf: bool = True) -> Path:
    out = data_dir / "applications" / slugify(f"{job['company']}-{job['title']}-{job['job_id']}")
    out.mkdir(parents=True, exist_ok=True)
    resume_html = render_resume_html(profile, tailored)
    (out / "resume.html").write_text(resume_html)
    if pdf:
        html_to_pdf(resume_html, out / "resume.pdf")
    (out / "cover_letter.txt").write_text(tailored.cover_letter.strip() + "\n")
    (out / "tailored.json").write_text(tailored.model_dump_json(indent=2))
    (out / "job.json").write_text(json.dumps({k: job[k] for k in job.keys()}, indent=2))
    return out


def load_tailored(materials_dir: Path) -> Tailored:
    """Reload the draft, picking up any edits you made to cover_letter.txt during review."""
    tailored = Tailored.model_validate_json((materials_dir / "tailored.json").read_text())
    tailored.cover_letter = (materials_dir / "cover_letter.txt").read_text().strip()
    return tailored


def cover_letter_pdf(materials_dir: Path, profile: Profile) -> Path:
    out = materials_dir / "cover_letter.pdf"
    paragraphs = "".join(
        f"<p>{html.escape(p)}</p>" for p in (materials_dir / "cover_letter.txt").read_text().split("\n\n")
    )
    html_to_pdf(f"<!doctype html><meta charset='utf-8'><style>{CSS} p{{margin:0 0 10px}}</style>"
                f"<h1>{html.escape(profile.person.full_name)}</h1>{paragraphs}", out)
    return out
