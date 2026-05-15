"""
Pre-submit review agent.

After the form-filling agent has populated every field, this module:
  1. Walks the DOM and extracts every visible field's label + current value.
  2. Sends those to Claude as a "second pair of eyes" along with the
     candidate's profile + resume.
  3. Returns a verdict (pass / warn / fail) plus a list of issues.

The caller (greenhouse.py et al) uses the verdict to decide whether to
click Submit. "fail" blocks the submission and routes the application to
the Needs Review tab with the issues attached as notes.

Cost: ~$0.01 per call with claude-haiku-4-5. ~10s added per apply.
Value: catches wrong gender, wrong work-authorization, hallucinated
schools, missed acknowledgment-Yes questions — the exact bugs we kept
hitting in production today.
"""
from __future__ import annotations
import asyncio
import json
import anthropic
from config import ANTHROPIC_API_KEY

# Use the same AsyncAnthropic client as the form-filler so we share the
# connection pool and don't fan out to a separate client.
_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ─── JS extractor: walks the form DOM ─────────────────────────────────
# This runs INSIDE the headless browser. It collects every filled-in
# field with its label and current value, skipping:
#   - hidden inputs (security tokens, CSRF, etc)
#   - submit / button / file inputs
#   - unchecked radios (only the chosen one matters)
#   - elements that aren't visible on screen
#
# We try several strategies for finding each field's label, in priority
# order: <label for=id>, aria-label, aria-labelledby, placeholder,
# nearest <label> ancestor, then `name` as a fallback.
_FORM_EXTRACT_JS = r"""
() => {
    const fields = [];
    const seenRadioGroups = new Set();

    // Helper: get the best human-readable label for an element
    function labelFor(el) {
        if (el.id) {
            const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (lbl) return lbl.innerText.trim();
        }
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel.trim();
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const lbl = document.getElementById(labelledBy);
            if (lbl) return lbl.innerText.trim();
        }
        // Nearest <label> ancestor
        let p = el.parentElement;
        for (let i = 0; i < 5 && p; i++) {
            if (p.tagName === 'LABEL') return p.innerText.trim();
            p = p.parentElement;
        }
        if (el.placeholder) return el.placeholder.trim();
        if (el.name) return el.name;
        return '';
    }

    function isVisible(el) {
        if (el.type === 'radio' || el.type === 'checkbox') return true;  // can be visually offscreen
        if (!el.offsetParent && el.tagName !== 'SELECT') return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden';
    }

    const selector = (
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="file"]),'
        + 'select, textarea'
    );

    for (const el of document.querySelectorAll(selector)) {
        if (!isVisible(el)) continue;
        const label = labelFor(el);
        let value = '';
        let kind = el.type || el.tagName.toLowerCase();

        if (el.tagName === 'SELECT') {
            // For <select>, prefer the option text (what the user sees)
            const opt = el.options[el.selectedIndex];
            value = opt ? (opt.text || opt.value) : '';
            if (!value || value.toLowerCase().includes('select')) continue;  // placeholder
        } else if (el.type === 'checkbox') {
            value = el.checked ? 'checked' : '';
            if (!value) continue;
        } else if (el.type === 'radio') {
            if (!el.checked) continue;  // only record the chosen radio
            // De-dupe radio groups so we get one entry per group, not one per radio
            const groupKey = el.name || el.id;
            if (seenRadioGroups.has(groupKey)) continue;
            seenRadioGroups.add(groupKey);
            // Use the chosen radio's label if available, otherwise the value
            const radioLbl = labelFor(el);
            value = radioLbl || el.value || 'selected';
            kind = 'radio';
        } else {
            value = (el.value || '').trim();
            if (!value) continue;
        }

        if (label) {
            fields.push({
                label: label.slice(0, 200),
                value: value.slice(0, 500),
                type: kind,
            });
        }
    }

    // Also extract custom React-Select / combobox-style dropdowns that
    // don't have native <select> elements. Common Greenhouse pattern:
    // a div with role="combobox" or class containing "react-select" that
    // shows the chosen option as text inside.
    const comboboxes = document.querySelectorAll(
        '[role="combobox"], [class*="react-select__single-value"]'
    );
    for (const el of comboboxes) {
        if (!el.innerText || !el.innerText.trim()) continue;
        // Find nearest label
        let label = '';
        let p = el.parentElement;
        for (let i = 0; i < 8 && p; i++) {
            const lbl = p.querySelector('label');
            if (lbl) { label = lbl.innerText.trim(); break; }
            p = p.parentElement;
        }
        if (!label) continue;
        const value = el.innerText.trim();
        // Skip placeholder-y values
        if (value.length > 200) continue;
        if (/^(select|choose|pick)\.{3}$/i.test(value)) continue;
        fields.push({ label: label.slice(0, 200), value: value.slice(0, 500), type: 'combobox' });
    }

    return fields;
}
"""


async def extract_filled_form_values(page_or_frame) -> list[dict]:
    """
    Run the JS extractor inside the page/frame and return the list of
    filled fields. Returns [] on any error (we never want this to crash
    the apply flow — the reviewer is best-effort).
    """
    try:
        result = await page_or_frame.evaluate(_FORM_EXTRACT_JS)
        return result or []
    except Exception as e:
        print(f"    ⚠ Reviewer: could not extract form values: {e}")
        return []


# ─── The review agent itself ──────────────────────────────────────────


_SYSTEM_PROMPT = """You are a quality-control reviewer for a job-application bot.

Another AI just filled out a job application form for a candidate. Your job
is to verify EVERY answer matches the candidate's actual profile + resume
before the form is submitted. Catch hallucinations, wrong genders, wrong
work authorization, wrong contact info, made-up schools, wrong acknowledgment
answers — anything that would embarrass the candidate or get them rejected.

You return a JSON object with three fields:
  verdict: "pass" | "warn" | "fail"
  issues:  [{field, entered, expected, severity}]
  summary: 1-sentence overall judgment

VERDICTS:
- "pass" = everything looks correct. Submit the application.
- "warn" = minor formatting / cosmetic differences. Submit anyway, but log.
- "fail" = at least one BLOCKER issue. Do NOT submit. The user will fix and retry.

BE PERMISSIVE on:
- Phone formatting: "9498700432" vs "(949) 870-0432" → fine
- Name order: "First Last" vs "Last, First" → fine
- Country variants: "United States" vs "USA" vs "US" → fine
- Address formatting: minor abbreviation differences are fine
- LinkedIn URL with or without trailing slash

BE STRICT on:
- Gender / pronouns: must match profile (BLOCKER if wrong)
- Work authorization: must match profile.work_auth (BLOCKER if wrong)
- Country of residence: must match profile (BLOCKER if wrong)
- Email + phone: must match profile (BLOCKER if wrong)
- Made-up schools or employers not in resume: BLOCKER
- Veteran / disability / race: must match profile preferences (decline-to-answer is always valid)
- Acknowledgment / compliance questions ("Company adheres to laws...", "I agree to background check", etc): should always be "Yes" — flag as BLOCKER if "No"
- Sponsorship questions: should be "No" if candidate is a US citizen or already authorized

For fields the form filler obviously did its best on (essay questions about motivation,
strengths, etc), evaluate for coherence not exact match — only flag if the answer is
obviously generic, irrelevant, or contradicts the resume.

Reply with ONLY valid JSON. No markdown, no explanations outside the JSON."""


async def review_form(
    field_values: list[dict],
    profile_text: str,
    company: str = "",
    job_title: str = "",
) -> dict:
    """
    Audit a filled form. Returns:
        {
          "verdict": "pass" | "warn" | "fail",
          "issues": [{"field": "...", "entered": "...", "expected": "...", "severity": "blocker" | "minor"}],
          "summary": "..."
        }

    On any error, returns {"verdict": "warn", ...} — so the apply proceeds.
    We never want the reviewer to block on its own infrastructure failures.
    """
    if not field_values:
        return {
            "verdict": "warn",
            "issues": [],
            "summary": "No filled fields detected — reviewer skipped.",
        }

    # Truncate profile to keep prompt size reasonable. profile_text already
    # has resume content appended by build_profile_text in apply.py.
    profile_excerpt = profile_text[:6000] if profile_text else "(no profile provided)"

    user_prompt = f"""Review this filled application for {job_title or "a role"} at {company or "an unknown company"}.

=== CANDIDATE PROFILE (with resume excerpt at the bottom) ===
{profile_excerpt}

=== FILLED APPLICATION ({len(field_values)} fields) ===
{json.dumps(field_values, ensure_ascii=False, indent=2)}

Audit every field. Return JSON only:
{{
  "verdict": "pass" | "warn" | "fail",
  "issues": [{{"field": "...", "entered": "...", "expected": "...", "severity": "blocker" | "minor"}}],
  "summary": "1-sentence verdict"
}}"""

    # RELIABILITY: retry on Anthropic's rate-limit (429) and overload (529)
    # responses, same shape as matcher.py's scorer. Previously a single
    # transient 429 → the reviewer returned "warn" and the apply submitted
    # without an audit. With 3 attempts (20s / 40s / 60s backoff) we ride
    # out brief capacity blips without blocking the apply flow on a real
    # outage. Non-retryable errors (JSON parse, schema, etc) still fall
    # through to "warn" immediately — retrying those would just waste tokens.
    for attempt in range(3):
        try:
            message = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw
                if raw.startswith("json"):
                    raw = raw[4:].strip()
                elif raw.startswith("\n"):
                    raw = raw.strip()
                # Remove trailing ``` if any leftover
                raw = raw.rsplit("```", 1)[0].strip()
            result = json.loads(raw)
            # Normalize the verdict + ensure required keys exist
            verdict = result.get("verdict", "warn").lower()
            if verdict not in ("pass", "warn", "fail"):
                verdict = "warn"
            result["verdict"] = verdict
            result.setdefault("issues", [])
            result.setdefault("summary", "")
            return result
        except json.JSONDecodeError as e:
            # Parsing failures aren't transient — retrying won't help.
            print(f"    ⚠ Reviewer returned invalid JSON: {e}")
            return {
                "verdict": "warn",
                "issues": [],
                "summary": "Reviewer response was unparseable — proceeding without verdict.",
            }
        except Exception as e:
            msg = str(e)
            is_rate_limited = ("rate_limit" in msg or "429" in msg or "529" in msg)
            if is_rate_limited and attempt < 2:
                wait = 20 * (attempt + 1)  # 20s, 40s
                print(f"    ⏳ Reviewer rate-limited — retrying in {wait}s "
                      f"(attempt {attempt+1}/3)...")
                await asyncio.sleep(wait)
                continue
            # Either non-retryable or we've exhausted the budget.
            print(f"    ⚠ Reviewer API call failed: {type(e).__name__}: {e}")
            return {
                "verdict": "warn",
                "issues": [],
                "summary": "Reviewer unavailable — proceeding without verdict.",
            }
    # Defensive: only reachable if we somehow exit the loop without returning.
    print(f"    ✗ Reviewer gave up after 3 attempts (rate limit)")
    return {
        "verdict": "warn",
        "issues": [],
        "summary": "Reviewer unavailable — proceeding without verdict.",
    }


async def run_pre_submit_review(
    page_or_frame,
    user_info: dict | None,
    profile_text: str | None,
    company: str = "",
    job_title: str = "",
    screenshot_prefix: str = "reviewer_blocked",
) -> bool:
    """
    The full pre-submit reviewer gate as one call. Encapsulates the pattern
    that lives inline in applier/greenhouse.py so the 5 other appliers can
    use it in 3 lines instead of 50.

    Returns:
        True  → the reviewer BLOCKED submission. The applier should bail
                out with `return "unknown"`. Notes (formatted issues) are
                stashed on `user_info["_reviewer_notes"]` so apply.py can
                surface them in the Needs Review tab.
        False → proceed with the submit (verdict was pass / warn, no
                blockers, OR the reviewer itself had an error — in which
                case we fail OPEN so a flaky reviewer never blocks real
                applies).

    The helper is best-effort: any exception inside it returns False and
    logs. Reviewer reliability shouldn't gate user submissions.
    """
    import time as _time
    try:
        print("    🔍 Reviewer auditing filled form before submit...")
        filled = await extract_filled_form_values(page_or_frame)
        if not filled:
            print("    ⚠ Reviewer skipped: no filled fields detected "
                  "(form may be in iframe we couldn't read)")
            return False

        print(f"    🔍 Reviewer found {len(filled)} filled fields — "
              f"sending to Claude...")
        verdict = await review_form(
            field_values=filled,
            profile_text=profile_text or "",
            company=company or "",
            job_title=job_title or "",
        )
        v = verdict.get("verdict", "warn")
        summary = verdict.get("summary", "")
        issues = verdict.get("issues", [])
        blockers = [i for i in issues if i.get("severity") == "blocker"]
        print(f"    🔍 Reviewer verdict: {v.upper()} — {summary}")
        for i in issues[:5]:
            sev = i.get("severity", "")
            marker = "✗" if sev == "blocker" else "⚠"
            print(f"      {marker} {(i.get('field') or '?')[:50]}: "
                  f"entered '{(i.get('entered') or '')[:50]}' "
                  f"(expected '{(i.get('expected') or '')[:50]}')")

        if v == "fail" and blockers:
            print(f"    ✗ Reviewer BLOCKED submission "
                  f"({len(blockers)} blockers) — routing to Needs Review")
            notes = format_issues_for_notes(verdict)
            if user_info is not None:
                user_info["_reviewer_notes"] = f"Reviewer blocked: {notes}"
            # Best-effort screenshot. Page has .screenshot(); Frame doesn't
            # (skip silently in that case).
            try:
                if hasattr(page_or_frame, "screenshot"):
                    await page_or_frame.screenshot(
                        path=f"screenshots/{screenshot_prefix}_{int(_time.time())}.png"
                    )
            except Exception:
                pass
            return True

        if v == "warn":
            print(f"    ⚠ Reviewer warnings but proceeding to submit "
                  f"(warnings, not blockers).")
        return False
    except Exception as _e:
        # Fail OPEN: never block a real apply on a reviewer infrastructure
        # bug. The user has already paid for tokens via the form-filling
        # agent; refusing to submit because OUR auditor crashed is worse
        # than submitting an audited-by-the-form-filler-only application.
        print(f"    ⚠ Reviewer error (continuing anyway): "
              f"{type(_e).__name__}: {_e}")
        return False


def format_issues_for_notes(verdict: dict, max_issues: int = 5) -> str:
    """
    Pretty-print a review verdict as a short string suitable for the
    `applications.notes` column the dashboard shows on the Needs Review tab.
    """
    summary = verdict.get("summary", "").strip()
    issues = verdict.get("issues", [])[:max_issues]
    if not issues:
        return summary or verdict.get("verdict", "unknown")
    lines = [summary] if summary else []
    for i in issues:
        field = (i.get("field") or "?")[:60]
        entered = (i.get("entered") or "")[:80]
        expected = (i.get("expected") or "")[:80]
        sev = i.get("severity", "")
        sev_marker = "✗" if sev == "blocker" else "⚠"
        lines.append(f"{sev_marker} {field}: entered '{entered}' (expected '{expected}')")
    return "\n".join(lines)[:1000]  # DB column is TEXT, but keep notes readable
