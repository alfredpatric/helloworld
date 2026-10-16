"""Fill and submit application forms in a real browser with Playwright.

Rather than hard-coding selectors per applicant tracking system, the agent reads
every field on the page (label, type, options, required), asks Claude to answer
them from your profile, and fills them in. It only clicks submit when every
required field has a confident answer; otherwise it hands the browser to you.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

# Tags every visible form field with data-ja-id and describes it for the model.
SCAN_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const ownText = el => {
    // Text of a container, minus the text of nested inputs/options.
    const c = el.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,[role=option],[role=listbox]').forEach(n => n.remove());
    return clean(c.innerText || c.textContent);
  };
  const labelOf = el => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean)
                  .map(n => n.innerText).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    let p = el.parentElement;
    for (let i = 0; p && i < 4; i++, p = p.parentElement) {
      const lab = p.querySelector('label, legend, [class*="label"], [class*="question"]');
      if (lab && clean(lab.innerText)) return clean(lab.innerText);
    }
    return clean(el.placeholder || el.name || el.id);
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    let p = el.parentElement;
    for (let i = 0; p && i < 6; i++, p = p.parentElement) {
      if (p.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1 || i > 1) {
        const lab = p.querySelector('legend, [class*="label"]:not(:has(input)), [class*="question"], label:not(:has(input))');
        if (lab && clean(lab.innerText)) return clean(lab.innerText);
        const t = ownText(p);
        if (t) return t.slice(0, 300);
      }
    }
    return clean(el.name);
  };
  const optionLabel = el => clean((el.labels && el.labels[0] && el.labels[0].innerText) ||
                                  el.getAttribute('aria-label') || el.value);

  const fields = [], groups = {};
  let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (type !== 'file' && !visible(el) && !(['radio', 'checkbox'].includes(type) && el.labels && el.labels.length && visible(el.labels[0]))) continue;
    if (el.closest('[aria-hidden="true"]') && type !== 'file') continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';

    if (type === 'radio' || type === 'checkbox') {
      const gkey = type + ':' + (el.name || ('anon' + n));
      if (!groups[gkey]) {
        const id = 'f' + (n++);
        groups[gkey] = { field_id: id, type: type === 'radio' ? 'radio' : 'checkbox',
                         label: groupLabel(el), required, options: [] };
        fields.push(groups[gkey]);
      }
      const g = groups[gkey];
      g.required = g.required || required;
      el.setAttribute('data-ja-id', g.field_id);
      el.setAttribute('data-ja-option', optionLabel(el));
      g.options.push(optionLabel(el));
      continue;
    }
    const id = 'f' + (n++);
    el.setAttribute('data-ja-id', id);
    const f = { field_id: id, label: labelOf(el), required };
    if (el.tagName === 'SELECT') {
      f.type = 'select';
      f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^select\b|^--/i.test(t));
    } else if (el.getAttribute('role') === 'combobox') {
      f.type = 'combobox';
    } else {
      f.type = el.tagName === 'TEXTAREA' ? 'textarea' : type;
      if (el.maxLength > 0 && el.maxLength < 100000) f.max_length = el.maxLength;
    }
    fields.push(f);
  }
  for (const f of fields) {
    if (/\*\s*$/.test(f.label)) { f.required = true; f.label = f.label.replace(/\s*\*\s*$/, ''); }
  }
  return fields;
}
"""

SUCCESS_RE = re.compile(
    r"thank you for (applying|your (application|interest))|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully submitted",
    re.I,
)
SUBMIT_RE = re.compile(r"^\s*(submit( application)?|send application|apply)\s*$", re.I)
CAPTCHA_SELECTOR = (
    "iframe[src*='recaptcha'][title*='challenge'], iframe[src*='hcaptcha'], "
    "iframe[src*='turnstile'], .g-recaptcha:not([data-size='invisible'])"
)


def launch(pw, headless: bool = True):
    """Start Chromium; JOB_AGENT_CHROMIUM points at a specific browser binary if needed."""
    path = os.environ.get("JOB_AGENT_CHROMIUM")
    return pw.chromium.launch(headless=headless, executable_path=path or None)


@dataclass
class FillResult:
    fields: list[dict]
    answers: dict[str, dict]
    open_questions: list[dict] = field(default_factory=list)  # needs a human
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.open_questions and not self.errors


@dataclass
class ApplyOutcome:
    status: str  # submitted | needs_manual | failed | dry_run
    detail: str
    fill: FillResult | None = None


def scan_fields(page: Page) -> list[dict]:
    fields = page.evaluate(SCAN_JS)
    for f in fields:
        if f["type"] == "combobox" and not f.get("options"):
            f["options"] = _combobox_options(page, f["field_id"])
    return [f for f in fields if f["label"] or f["type"] == "file"]


def _combobox_options(page: Page, field_id: str) -> list[str]:
    box = page.locator(f'[data-ja-id="{field_id}"]')
    try:
        box.click(timeout=2000)
        page.wait_for_timeout(300)
        options = [t.strip() for t in page.locator("[role=option]").all_inner_texts() if t.strip()]
        box.press("Escape")
        return options[:50]
    except PWTimeout:
        return []


def _open_form(page: Page) -> list[dict]:
    fields = scan_fields(page)
    if len([f for f in fields if f["type"] != "file"]) >= 2:
        return fields
    # Some job pages show the description first and reveal the form behind an Apply button.
    for name in ("Apply for this job", "Apply now", "Apply"):
        button = page.get_by_role("link", name=name).or_(page.get_by_role("button", name=name)).first
        if button.count():
            button.click()
            page.wait_for_load_state("networkidle")
            return scan_fields(page)
    return fields


def fill_field(page: Page, f: dict, value: str, files: dict[str, Path]) -> None:
    loc = page.locator(f'[data-ja-id="{f["field_id"]}"]')
    kind = f["type"]
    if kind == "file":
        path = files.get(value)
        if not path or not path.exists():
            raise FileNotFoundError(f"no {value} file to upload")
        loc.set_input_files(str(path))
        page.wait_for_timeout(1500)  # some forms parse the resume and prefill fields
    elif kind == "select":
        try:
            loc.select_option(label=value, timeout=3000)
        except PWTimeout:
            loc.select_option(value=value, timeout=3000)
    elif kind == "radio":
        page.locator(f'[data-ja-id="{f["field_id"]}"][data-ja-option="{_css(value)}"]').first.check(force=True)
    elif kind == "checkbox":
        chosen = {v.strip().lower() for v in value.split("|") if v.strip()}
        if len(f["options"]) == 1 and value.lower() in ("yes", "true", "checked", "agree"):
            chosen = {f["options"][0].lower()}
        for option in f["options"]:
            if option.lower() in chosen:
                page.locator(f'[data-ja-id="{f["field_id"]}"][data-ja-option="{_css(option)}"]').first.check(force=True)
    elif kind == "combobox":
        loc.click()
        loc.fill(value)
        page.wait_for_timeout(400)
        option = page.get_by_role("option", name=value, exact=True)
        if option.count():
            option.first.click()
        else:
            loc.press("Enter")
    else:
        loc.fill(value)


def _css(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def fill_form(page: Page, answer_fn, files: dict[str, Path]) -> FillResult:
    fields = _open_form(page)
    if not fields:
        return FillResult([], {}, errors=["No application form found on the page"])
    answers = {a.field_id: a.model_dump() for a in answer_fn(fields).answers}
    result = FillResult(fields, answers)
    # Upload files first: resume parsers may overwrite text fields, which we then fill.
    for f in sorted(fields, key=lambda f: f["type"] != "file"):
        a = answers.get(f["field_id"])
        value = (a or {}).get("value", "").strip()
        if a and not a["confident"]:
            result.open_questions.append({**f, "suggestion": value, "note": a["note"]})
        elif f["required"] and not value:
            result.open_questions.append({**f, "suggestion": "", "note": "required but left blank"})
        if not value:
            continue
        try:
            fill_field(page, f, value, files)
        except Exception as e:  # keep going; report what failed
            result.errors.append(f"{f['label']!r}: {type(e).__name__}: {str(e).splitlines()[0]}")
    return result


def _click_submit(page: Page) -> bool:
    candidates = page.locator("button[type=submit], input[type=submit]").or_(
        page.get_by_role("button", name=SUBMIT_RE)
    )
    for i in range(candidates.count()):
        c = candidates.nth(i)
        if c.is_visible():
            c.click()
            return True
    return False


def _submission_succeeded(page: Page, start_url: str) -> tuple[bool, str]:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    body = page.inner_text("body")
    if SUCCESS_RE.search(body) or re.search(r"confirm|thank", page.url, re.I):
        return True, "confirmation shown"
    if page.locator(CAPTCHA_SELECTOR).count():
        return False, "CAPTCHA challenge"
    invalid = page.locator("[aria-invalid=true]").count()
    if invalid:
        return False, f"{invalid} field(s) flagged invalid by the form"
    if page.url != start_url:
        return False, f"page changed to {page.url} without a clear confirmation"
    return False, "no confirmation after clicking submit"


def apply(
    url: str,
    answer_fn,
    files: dict[str, Path],
    out_dir: Path,
    *,
    headed: bool = False,
    submit: bool = True,
    confirm_fn=None,
) -> ApplyOutcome:
    """Open the application, fill it, and submit when allowed.

    submit=False is a dry run. In headed mode with confirm_fn, the browser stays
    open after filling so you can fix anything and press submit yourself; confirm_fn
    then asks you whether it went through.
    """
    with sync_playwright() as pw:
        browser = launch(pw, headless=not headed)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            result = fill_form(page, answer_fn, files)
            (out_dir / "form.json").write_text(json.dumps(
                {"url": url, "fields": result.fields, "answers": result.answers,
                 "open_questions": result.open_questions, "errors": result.errors}, indent=2))
            page.screenshot(path=str(out_dir / "filled.png"), full_page=True)

            if not result.fields:
                return ApplyOutcome("failed", result.errors[0], result)
            if not submit:
                return ApplyOutcome("dry_run", f"filled {len(result.answers)} fields; not submitted", result)
            if headed and confirm_fn:
                return ApplyOutcome(*confirm_fn(result), result)
            if not result.ready:
                return ApplyOutcome("needs_manual", _summary(result), result)
            if page.locator(CAPTCHA_SELECTOR).count():
                return ApplyOutcome("needs_manual", "CAPTCHA on page", result)

            start = page.url
            if not _click_submit(page):
                return ApplyOutcome("needs_manual", "could not find the submit button", result)
            ok, why = _submission_succeeded(page, start)
            page.screenshot(path=str(out_dir / "after_submit.png"), full_page=True)
            return ApplyOutcome("submitted" if ok else "needs_manual", why, result)
        except PWTimeout as e:
            return ApplyOutcome("failed", f"timed out: {str(e).splitlines()[0]}")
        finally:
            browser.close()


def _summary(result: FillResult) -> str:
    parts = [f"open question: {q['label'][:80]}" for q in result.open_questions]
    parts += [f"fill error: {e}" for e in result.errors]
    return "; ".join(parts)
