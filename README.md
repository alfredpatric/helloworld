# Job application agent (semi-automatic)

An agent that finds roles that fit you, tailors your resume and cover letter for each one, and fills in the application form. You stay in control:

- **Strong fits with no open questions** can be submitted automatically. This is off by default.
- **Everything else** waits in a review queue for you to approve, edit or reject.
- **Anything it isn't sure about** (a legal question missing from your profile, a CAPTCHA, an unusual form) is handed back to you in a browser window, filled in as far as it got.

It never invents experience. Company names, titles and dates always come from your profile. The model only picks and rephrases your own bullets, and it answers legal questions (work authorization, sponsorship, salary) only from answers you wrote yourself.

## How it works

```
discover ─► triage ─────────────► review (you) ─► apply ─► tracker
 job board   prefilter → score      approve/edit/     fill form, submit
 APIs        → draft materials      reject             or hand to you
```

1. **Discover**: pulls open roles from the public job-board feeds of the companies you list (Greenhouse, Lever, Ashby). These are the same feeds that power their careers pages, so there's no scraping or login. Roles it has already seen are ignored.
2. **Triage**: a free keyword prefilter (titles, locations, companies to avoid) runs first. Claude then scores the remaining roles from 0 to 100 against your profile and dealbreakers. For good fits it writes a tailored resume (PDF) and cover letter into `data/applications/<role>/`.
3. **Review**: `review` walks you through each draft. Approve, reject, or edit `cover_letter.txt` before approving.
4. **Apply**: opens the application in Chromium, reads every field on the form, and has Claude answer each one from your profile. It uploads your files and submits **only if every required field has a confident answer**. Otherwise the role is marked `needs_manual`.
5. **Track**: everything is stored in `data/jobs.db`, including scores, the answers given, and screenshots of the form before and after submitting.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...      # or: ant auth login

python -m job_agent init                 # creates profile.yaml and config.yaml
```

Fill in **`profile.yaml`** thoroughly. It's the agent's only source of truth. Include every role and every achievement you might want to use; it picks the right ones for each job. The more `standard_answers` you fill in, the more forms it can finish without you.

In **`config.yaml`**, list the companies you want to watch, using their careers-page slugs:

| Board      | URL looks like                        | slug     |
|------------|----------------------------------------|----------|
| Greenhouse | `job-boards.greenhouse.io/stripe`      | `stripe` |
| Lever      | `jobs.lever.co/plaid`                  | `plaid`  |
| Ashby      | `jobs.ashbyhq.com/ramp`                | `ramp`   |

Both files are gitignored because they contain your personal data.

## Daily use

```bash
python -m job_agent run --no-submit      # discover + score + draft
python -m job_agent review               # approve / reject drafts
python -m job_agent apply --dry-run      # optional: fill forms, screenshot, don't submit
python -m job_agent apply                # submit what you approved
python -m job_agent status               # see the pipeline
```

To finish an application the agent handed back:

```bash
python -m job_agent apply --headed greenhouse:stripe:4012345
```

A browser window opens with the form already filled in. The terminal lists the questions it wasn't sure about. Fix those, click submit yourself, then confirm in the terminal.

Once you get replies, record them with `python -m job_agent mark <key> interviewing` (or `offer` / `rejected_by_company`).

### Turning on auto-submit

After a couple of weeks, once the drafts are consistently good, set `policy.auto_submit: true` in `config.yaml`. Then roles scoring at or above `auto_submit_threshold` (default 85) skip review and are submitted by `run`, subject to the `max_applications_per_day` cap. They're still only submitted when every form question has a confident answer.

### Scheduling

Run it every weekday morning with cron (`crontab -e`):

```
0 8 * * 1-5  cd /path/to/helloworld && .venv/bin/python -m job_agent run >> data/run.log 2>&1
```

## Costs and model

Calls go to Claude (`claude-opus-5-5` by default, set in `config.yaml`). Each scored role is one short call. Each drafted role adds one call for tailoring and one per form. Your profile is cached between calls. `max_jobs_scored_per_run` caps spending per run. Use the free prefilter (`target_titles`, `title_exclude`, `locations`) to keep obvious misses away from the model.

## Limits and etiquette

- **Supported**: Greenhouse, Lever and Ashby forms. Workday and LinkedIn Easy Apply aren't supported. Workday needs an account per company, and LinkedIn prohibits automation, so applying that way risks your account.
- **CAPTCHAs**: any challenge sends the application to `needs_manual`. The agent never tries to get around one.
- **Volume**: a few well-matched, tailored applications beat a hundred generic ones. Keep the daily cap modest and skim what it sends.

## Development

```bash
pip install pytest
python -m pytest tests
```

The tests use recorded job-board responses and a local practice application form, with Claude stubbed out. Set `JOB_AGENT_CHROMIUM=/path/to/chrome` to use a specific browser binary.
