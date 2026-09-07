"""
automation/return_status.py
=============================
F-67 — check a filed return's current ITD processing status for one client,
one Assessment Year, WITHOUT downloading any documents. Deliberately a
lighter-weight sibling of automation/downloader_filed_returns.py, reusing its
already-confirmed navigation and AY-filter helpers (navigate_to_view_filed_
returns, apply_ay_filter) and several of its private per-card readers
(_read_filing_date, _read_ack_no, _is_discarded, _pager_arrow_enabled,
_goto_page) rather than duplicating that logic — see this app's own repeated
"Form 168 emailer bug" lesson (automation/doc_types.py's docstring) on why
two copies of the same scraping logic drift apart.

Same "most recent filing wins" rule as downloader_filed_returns.py's
filing_scope="latest": one AY can have more than one filing (Original,
Revised, Rectification, ...) — this always reports the status of whichever
one has the latest Filing Date, since that's the one that actually reflects
where things currently stand.
"""

import asyncio

from playwright.async_api import Page

from automation.downloader import make_step_logger
from automation.downloader_filed_returns import (
    navigate_to_view_filed_returns,
    apply_ay_filter,
    _read_filing_date,
    _read_ack_no,
    _is_discarded,
    _pager_arrow_enabled,
    _goto_page,
    _INTIMATION_DATE_RE,
)


async def _read_latest_status(card, step) -> tuple[str, str]:
    """Confirmed markup (shared with _is_discarded/_is_return_verified in
    downloader_filed_returns.py): one .matStepStatus element per step on the
    filing's status timeline. BUG FIX (2026-09-07): confirmed live (a real
    portal screenshot, client Santosh Kumari Malik, AY 2026-27) — the
    timeline renders NEWEST-first, not chronological: "Processed with no
    demand/refund" (Aug 3) appeared above "Successfully e-Verified" (Jul 29)
    which appeared above "Pending for e-Verification" (also Jul 29, the
    earliest step). The original code took the LAST step assuming
    oldest-to-newest order, which reported "Pending for e-Verification" —
    the very first, most outdated step — as the client's current status,
    even though the return was actually already fully processed. Now reads
    the FIRST step instead, which is the most advanced one actually
    reached.

    Returns (status_text, status_date) — status_date is the date the
    PORTAL itself shows against that step (e.g. "Aug 3, 2026"), read from
    the step's own container text via the same date regex already used for
    Intimation Order dates, since the date isn't inside .matStepStatus
    itself (confirmed live: that element's own text is just the status
    label, e.g. "ITR Filed" with no date). UNCONFIRMED, flagged for live
    testing: the container is assumed to be .matStepStatus's immediate
    parent — if a live check shows the date living somewhere else in the
    step's markup, this is the one thing to fix here."""
    try:
        statuses = card.locator(".matStepStatus")
        count = await statuses.count()
        if count == 0:
            step("No .matStepStatus steps found on this card")
            return "", ""
        latest = statuses.nth(0)
        text = (await latest.inner_text() or "").strip()
        step(f"Latest status step ({count} total): '{text}'")

        status_date = ""
        try:
            container_text = await latest.locator("xpath=..").inner_text()
            m = _INTIMATION_DATE_RE.search(container_text)
            if m:
                status_date = m.group(1)
                step(f"Latest status date: '{status_date}'")
            else:
                step(f"No date found in status step container text: '{container_text}'")
        except Exception as e:
            step(f"Could not read status step date: {e}")

        return text, status_date
    except Exception as e:
        step(f"Could not read status stepper: {e}")
        return "", ""


async def scan_latest_status(filed_returns_page, assessment_year: str, step) -> dict:
    """The card-scan + 'most recent filing wins' resolution + status/date
    read, factored out of check_return_status() so it can be reused by
    ANY caller that has already navigated to View Filed Returns and
    applied the AY filter for this year — not just check_return_status()
    itself. In particular, automation/downloader_filed_returns.py's
    download flow calls this too (F-67 "kill two birds" follow-up): it
    has already navigated + AY-filtered the page as part of downloading
    that year's documents, so reusing that same page here avoids a
    second, separate navigate+filter (the expensive/fragile part) just
    to also record processing status — only the comparatively cheap
    per-card scan below is repeated.

    Returns {"ok", "reason", "status", "status_date", "filing_date", "ack_no"}
    — see check_return_status()'s docstring for what each field means."""
    empty = {"ok": False, "reason": "", "status": "", "status_date": "", "filing_date": "", "ack_no": ""}
    next_page_btn = filed_returns_page.locator("img[alt='next page']").first
    prev_page_btn = filed_returns_page.locator("img[alt='previous page']").first

    # Reset to the first page before scanning. This function's own paging
    # below always starts counting from page 0 — fine when called right
    # after apply_ay_filter() (which lands on page 0 already), but NOT
    # safe when called after a download pass that may have paged forward
    # to find/download a specific filing and left the pager there. Without
    # this, the scan below could start mid-way through the results and
    # miss earlier cards, or double-count pages already stepped through.
    for _reset_attempt in range(10):
        try:
            if await _pager_arrow_enabled(prev_page_btn, step, "previous"):
                await prev_page_btn.click()
                await asyncio.sleep(1)
            else:
                break
        except Exception as e:
            step(f"Pager reset-to-first-page failed (continuing): {e}")
            break

    candidates: list[dict] = []
    current_page = 0
    for _page_num in range(10):  # same hard cap as downloader_filed_returns.py
        step(f"Scanning page {current_page} for AY {assessment_year} cards")
        all_cards = filed_returns_page.locator("mat-card.contextBox")
        card_count = await all_cards.count()
        for i in range(card_count):
            card = all_cards.nth(i)
            try:
                ay_text = await card.locator(".contentHeadingText").first.inner_text()
            except Exception as e:
                step(f"Card {i}: could not read AY heading ({e})")
                ay_text = ""
            if assessment_year not in ay_text:
                continue
            if await _is_discarded(card, step):
                step(f"Card {i}: discarded — excluded from 'latest' comparison")
                continue
            date_text, date_parsed = await _read_filing_date(card, step)
            candidates.append({
                "page": current_page, "index": i,
                "date_text": date_text, "date_parsed": date_parsed,
            })

        try:
            if await _pager_arrow_enabled(next_page_btn, step, "next"):
                await next_page_btn.click()
                await asyncio.sleep(1)
                current_page += 1
            else:
                break
        except Exception as e:
            step(f"Next-page click failed ({e}) — stopping scan")
            break

    if not candidates:
        step(f"No non-discarded filing found for AY {assessment_year}")
        return {**empty, "reason": f"No filing found for AY {assessment_year}"}

    dated = [c for c in candidates if c["date_parsed"] is not None]
    winner = max(dated, key=lambda c: c["date_parsed"]) if dated else candidates[0]
    step(f"Latest filing: page={winner['page']}, index={winner['index']}, date={winner['date_text']}")

    await _goto_page(winner["page"], current_page, next_page_btn, prev_page_btn, step)
    card = filed_returns_page.locator("mat-card.contextBox").nth(winner["index"])
    ack_no = await _read_ack_no(card, step)
    status_text, status_date = await _read_latest_status(card, step)

    if not status_text:
        return {**empty, "reason": "Found the filing but could not read its status"}

    return {
        "ok": True, "reason": "", "status": status_text, "status_date": status_date,
        "filing_date": winner["date_text"], "ack_no": ack_no,
    }


async def check_return_status(page: Page, assessment_year: str, log_callback,
                               pan: str = "", dob: str = "") -> dict:
    """One already-logged-in client, one Assessment Year. Returns:
    {"ok": bool, "reason": str, "status": str, "status_date": str,
     "filing_date": str, "ack_no": str}
    ok=False covers: AY filter didn't apply, no (non-discarded) filing found
    for this AY, or a navigation/timeout failure — reason has a short
    human-readable explanation in every ok=False case. status is the raw
    text of the latest filing's current stepper step (e.g. "ITR processed",
    "Successfully e-Verified", "Pending for e-Verification") — shown exactly
    as the portal itself shows it, never simplified or remapped. status_date
    is the date the PORTAL shows against that same step (e.g. "Aug 3,
    2026") — distinct from filing_date (when the return was filed) and from
    whatever timestamp the caller records for "when this app last checked."""
    step = make_step_logger(log_callback, "RETSTATUS")
    empty = {"ok": False, "reason": "", "status": "", "status_date": "", "filing_date": "", "ack_no": ""}
    try:
        step(f"Starting return status check — AY={assessment_year}, pan={'set' if pan else 'blank'}")
        filed_returns_page = await navigate_to_view_filed_returns(page, log_callback)

        filtered = await apply_ay_filter(filed_returns_page, assessment_year, step)
        if not filtered:
            # Unlike the full download flow, a status-only check has no
            # reason to fall back to a full page-walk scan — the AY filter
            # is cheap and reliable; if it didn't apply, something on the
            # portal side is off and guessing at a status would be worse
            # than just reporting the failure plainly.
            step("AY filter did not apply — reporting failure rather than guessing")
            return {**empty, "reason": f"Could not filter to Assessment Year {assessment_year} on the portal"}

        return await scan_latest_status(filed_returns_page, assessment_year, step)
    except Exception as e:
        err = str(e)
        step(f"[Error] Return status check failed: {err}")
        if "Timeout" in err or "timeout" in err:
            reason = "Timed out waiting for portal response (try again)"
        elif "net::" in err.lower():
            reason = "Network error — check internet connection"
        elif "Target page" in err or "browser has been closed" in err:
            reason = "Browser closed unexpectedly"
        else:
            reason = err[:80] if len(err) <= 80 else err[:77] + "..."
        return {**empty, "reason": reason}
