import asyncio
import os
import re
from datetime import datetime

from playwright.async_api import Page

from automation.downloader import update_browser_status, make_step_logger
from automation._nav_helpers import open_hamburger, hover_to_income_tax_returns
from automation.pdf_unlocker import unlock_pdf
from utils import migrate_flat_docs_to_subfolders, migrate_itr_filing_subfolder

_FILING_TYPE_MAP = {
    "original": "Original",
    "revised": "Revised",
    "defective": "Defective",
    "rectification": "Rectification",
    "updated": "Updated",
    "modified": "Modified",
}

_DATE_FORMATS = ("%b %d, %Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y")
_INTIMATION_DATE_RE = re.compile(
    r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}|\d{1,2}[-/][A-Za-z]{3}[-/]\d{4}|\d{2}[-/]\d{2}[-/]\d{4})"
)


def _sanitize_filing_type(raw_label: str, step) -> str:
    normalized = (raw_label or "").strip().lower()
    for key, canonical in _FILING_TYPE_MAP.items():
        if key in normalized:
            step(f"Filing Type '{raw_label}' -> '{canonical}'")
            return canonical
    step(f"Unrecognized Filing Type label '{raw_label}' — using fallback 'Filing'")
    return "Filing"


def _sanitize_date_for_filename(raw_date: str, step) -> str:
    raw_date = (raw_date or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw_date, fmt).strftime("%Y_%m_%d")
            step(f"Parsed date '{raw_date}' using format '{fmt}' -> {parsed}")
            return parsed
        except ValueError:
            continue
    step(f"Could not parse date '{raw_date}' with any known format — using today's date instead")
    return datetime.now().strftime("%Y_%m_%d")


async def _read_filing_date(card, step) -> tuple[str, "datetime | None"]:
    """Confirmed markup: a div.valueBox containing a rightsideLabel reading
    'Filing Date' and a sibling fieldVal holding the value, e.g. 'Oct 11, 2024'."""
    try:
        raw_text = await card.locator(
            "div.valueBox:has(mat-label.rightsideLabel:has-text('Filing Date')) mat-label.fieldVal"
        ).first.inner_text()
    except Exception as e:
        step(f"Could not read Filing Date field: {e}")
        raw_text = ""
    raw_text = (raw_text or "").strip()
    step(f"Raw Filing Date text: '{raw_text}'")
    parsed = None
    match = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", raw_text)
    if match:
        try:
            parsed = datetime.strptime(match.group(1), "%b %d, %Y")
            step(f"Parsed Filing Date: {parsed.date()}")
        except ValueError:
            step(f"Filing Date regex matched '{match.group(1)}' but strptime failed")
    else:
        step("Filing Date text did not match expected 'Mon D, YYYY' pattern")
    return raw_text, parsed


async def _read_ack_no(card, step) -> str:
    """Confirmed markup: a div.valueBox containing a rightsideLabel reading
    'Acknowledgement No' and a sibling fieldVal holding the value. Used to
    guarantee unique per-filing subfolders even when two filings share the
    same Filing Type AND Filing Date (confirmed live: a discarded-then-
    refiled 'Original' return can share both with the return that replaced
    it) — the Acknowledgement Number is unique per filing submission."""
    try:
        raw_text = await card.locator(
            "div.valueBox:has(mat-label.rightsideLabel:has-text('Acknowledgement No')) mat-label.fieldVal"
        ).first.inner_text()
    except Exception as e:
        step(f"Could not read Acknowledgement No field: {e}")
        raw_text = ""
    ack_no = (raw_text or "").strip()
    step(f"Acknowledgement No: '{ack_no}'")
    return ack_no


async def _is_discarded(card, step) -> bool:
    """Confirmed live: a filing's status stepper can read 'The ITR filed has
    been discarded by the Taxpayer, hence cannot be verified anymore.' —
    legally treated as never filed. Checks every status step on the card
    (not just the latest) since the discarded notice's position among the
    steps isn't guaranteed."""
    try:
        statuses = card.locator(".matStepStatus")
        count = await statuses.count()
        for i in range(count):
            text = (await statuses.nth(i).inner_text() or "").strip()
            if "discard" in text.lower():
                step(f"Filing status indicates discarded: '{text}'")
                return True
    except Exception as e:
        step(f"Could not check discarded status: {e}")
    return False


async def _is_return_verified(card, step) -> "bool | None":
    """Confirmed live: the 'Download Receipt' button serves two entirely
    different PDFs depending on verification status, even though it's the
    same button/link — 'INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT' once
    e-verified, or 'INDIAN INCOME TAX RETURN VERIFICATION FORM' (ITR-V) if
    still pending. Checks the status stepper for known phrases. Returns True
    (verified), False (confirmed still pending), or None (unclear — caller
    should default to the more common "Receipt" label rather than guess
    wrong in the other direction)."""
    try:
        statuses = card.locator(".matStepStatus")
        count = await statuses.count()
        texts = [(await statuses.nth(i).inner_text() or "").strip().lower() for i in range(count)]
        # Confirmed per user: any processing-stage status ("under processing",
        # "processed with demand/refund due", "demand adjusted...", etc.) is
        # only reachable AFTER verification — the portal cannot process an
        # unverified return. So these are just as strong a "verified" signal
        # as an explicit e-verification/EVC status, and take precedence over
        # a "pending" step appearing earlier in the same timeline.
        if any(
            "successfully e-verified" in t or "evc accepted" in t
            or "processing" in t or "processed" in t
            for t in texts
        ):
            step(f"Filing status indicates verified: {texts}")
            return True
        if any("pending for e-verification" in t for t in texts):
            step(f"Filing status indicates NOT yet verified: {texts}")
            return False
        step(f"Filing verification status unclear from steps: {texts}")
    except Exception as e:
        step(f"Could not check verification status: {e}")
    return None


async def apply_ay_filter(filed_returns_page: Page, assessment_year: str, step,
                            previous_year: str | None = None) -> bool:
    """Opens the Filter popup and selects assessment_year in the 'Assessment
    Year' multi-select, then applies it — so the list renders only matching
    filings instead of needing a full page-by-page scan. Confirmed: the
    control is a standard Angular Material multi-select
    (mat-select[formcontrolname='ay']), rendering its options as
    <mat-option aria-label="{year} checkbox"> in a floating overlay panel
    (not inside the <mat-select> element itself). Returns True if the filter
    was applied, False if anything about the popup didn't match expectations
    (caller should fall back to the page-walk scan in that case).

    F-14 (multi-year): `previous_year`, if given, is unchecked before the new
    year is checked — since this is a multi-select, looping this function
    across several years without unchecking the prior one would leave every
    previously-selected year still active as a filter criterion (this was
    never an issue before, since the page was only ever filtered once per
    load)."""
    try:
        step("Locating toolbar Filter button (#filterbtn1/#filterbtn3)")
        filter_btn = filed_returns_page.locator("#filterbtn1, #filterbtn3").first
        step("Clicking toolbar Filter button")
        await filter_btn.click()
        await asyncio.sleep(0.5)

        step("Waiting for Assessment Year mat-select to be visible")
        ay_select = filed_returns_page.locator("mat-select[formcontrolname='ay']").first
        await ay_select.wait_for(state="visible", timeout=10000)
        step("Clicking Assessment Year dropdown to open option panel")
        await ay_select.click()

        if previous_year and previous_year != assessment_year:
            try:
                prev_option = filed_returns_page.locator(
                    f"mat-option[aria-label='{previous_year} checkbox']"
                ).first
                if await prev_option.count() > 0:
                    is_checked = await prev_option.locator(".mat-pseudo-checkbox-checked").count() > 0
                    if is_checked:
                        step(f"Unchecking previous year filter: {previous_year}")
                        await prev_option.click()
                        await asyncio.sleep(0.3)
                    else:
                        step(f"Previous year '{previous_year}' filter option not shown as checked — skipping uncheck")
                else:
                    step(f"Previous year '{previous_year}' option not found in panel (may need 'View More') — skipping uncheck")
            except Exception as e:
                step(f"Could not uncheck previous year filter option (continuing): {e}")

        # Older assessment years (e.g. 2016-17) are NOT rendered in the option
        # panel by default — a "View More" inside the panel itself reveals
        # them. TODO: exact markup unconfirmed (the option panel dump we have
        # only shows recent years with a trailing Angular placeholder comment
        # where a conditional "View More" would render) — this is a
        # best-effort click, harmless no-op via timeout if absent/wrong.
        try:
            options_view_more = filed_returns_page.locator(
                "[role='listbox'] >> text=/^View More$/i"
            ).first
            if await options_view_more.is_visible(timeout=2000):
                step("Year dropdown 'View More' found — clicking to reveal older years")
                await options_view_more.click()
                await asyncio.sleep(0.5)
            else:
                step("Year dropdown 'View More' not visible — assuming target year already listed")
        except Exception as e:
            step(f"Year dropdown 'View More' check skipped: {e}")

        # Confirmed exact markup: <mat-option aria-label="2024-25 checkbox">
        option_selector = f"mat-option[aria-label='{assessment_year} checkbox']"
        step(f"Waiting for year option: {option_selector}")
        year_option = filed_returns_page.locator(option_selector).first
        await year_option.wait_for(state="visible", timeout=10000)
        step(f"Clicking year option '{assessment_year}'")
        await year_option.click()

        step("Pressing Escape to close option overlay (selection persists)")
        await filed_returns_page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

        # Confirmed exact markup: the popup's own apply button is
        # <button id="okButton">Filter</button> in its mat-card-footer —
        # distinct from the toolbar button (#filterbtn1/#filterbtn3) that
        # opened the popup.
        step("Clicking popup apply button (#okButton)")
        apply_btn = filed_returns_page.locator("#okButton")
        await apply_btn.click()
        await asyncio.sleep(1.5)
        step(f"Assessment Year filter applied successfully: {assessment_year}")
        return True
    except Exception as e:
        step(f"Assessment Year filter failed: {e} — will fall back to full page scan")
        # Confirmed root cause of a follow-on bug: if the failure happened
        # while the year-dropdown's option panel was still open (e.g. the
        # target year's mat-option never appeared and the wait timed out),
        # its CDK overlay backdrop is left covering the whole page and
        # silently intercepts every subsequent click — including the
        # page-level "View more" button the caller tries next. Press Escape
        # (twice, to also dismiss the outer Filter popup) before returning,
        # so the page is left clean for the fallback scan.
        try:
            await filed_returns_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await filed_returns_page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            step("Pressed Escape twice to clear any leftover overlay/popup")
        except Exception as cleanup_err:
            step(f"Escape cleanup failed (continuing): {cleanup_err}")
        return False


async def _pager_arrow_enabled(arrow_locator, step, label: str) -> bool:
    """The pager's prev/next controls are <img alt="next page"/"previous
    page"> elements (not real <button>s), so Playwright's is_enabled() does
    not apply. Confirmed: a disabled arrow carries aria-disabled="true" (and
    a "...DisableLight.svg" src, which we don't need to check separately)."""
    try:
        if await arrow_locator.count() == 0:
            step(f"Pager arrow '{label}' not found in DOM")
            return False
        disabled = await arrow_locator.get_attribute("aria-disabled")
        enabled = disabled != "true"
        step(f"Pager arrow '{label}' aria-disabled='{disabled}' -> enabled={enabled}")
        return enabled
    except Exception as e:
        step(f"Pager arrow '{label}' check failed: {e}")
        return False


async def _goto_page(target_page: int, current_page: int, next_btn, prev_btn, step) -> int:
    """Step the paginator from current_page to target_page (both 0-based),
    clicking next/previous the required number of times. Returns the page
    actually landed on (may differ from target_page if a click silently
    no-ops, e.g. already at an edge)."""
    step(f"Navigating pager from page {current_page} to page {target_page}")
    while current_page < target_page:
        await next_btn.click()
        await asyncio.sleep(1)
        current_page += 1
        step(f"Clicked next — now on page {current_page}")
    while current_page > target_page:
        await prev_btn.click()
        await asyncio.sleep(1)
        current_page -= 1
        step(f"Clicked previous — now on page {current_page}")
    return current_page


async def _download_one_file(page: Page, trigger_locator, output_path: str, step, artifact_label: str,
                              pan: str = "", dob: str = "") -> bool:
    """Wraps a single expect_download()+save_as() in try/except so one failed
    artifact never aborts the remaining filings/artifacts in the batch.
    If dob is provided and the saved file is a PDF, attempts to auto-unlock
    it via pdf_unlocker.unlock_pdf — that function itself cheaply no-ops on
    files that turn out not to be encrypted, so it's safe to call for every
    PDF artifact rather than needing to know in advance which ones ITD
    protects (confirmed: Intimation Orders are; Form/Receipt may or may not
    be, so we don't special-case)."""
    try:
        step(f"Clicking trigger for {artifact_label}, expecting a download")
        async with page.expect_download() as download_info:
            await trigger_locator.click()
        step(f"Download event received for {artifact_label} — saving to {output_path}")
        await (await download_info.value).save_as(output_path)
        step(f"[Victory] {artifact_label} downloaded: {os.path.basename(output_path)}")
        if dob and output_path.lower().endswith(".pdf"):
            result = unlock_pdf(output_path, pan=pan, dob=dob, log=step)
            if result.get("unlocked"):
                step(f"[PDF Unlock] {os.path.basename(output_path)} unlocked (password: {result.get('password')})")
            else:
                step(f"[PDF Unlock] {os.path.basename(output_path)} left as-is: {result.get('reason')}")
        return True
    except Exception as e:
        step(f"[Warning] {artifact_label} download failed: {e}")
        return False


async def _submit_intimation_request(card, filing_type: str, step) -> str | None:
    """Handles the alternate 'Submit Intimation Request/Download Intimation
    Order' link seen on older/defective filings whose intimation hasn't been
    generated yet — confirmed via live screenshots to trigger one of two
    modal flows:
      - #confirmSubIntimation (fresh request): Yes/No buttons -> clicking Yes
        opens #downldIntimationFortyEight (confirmation, single Close button
        #yesButton1).
      - #downldIntimation (already requested earlier): single Close button
        #yesButton.
    No polling/waiting is attempted — the department can take hours to a day
    to generate it. Once ready, this same card will show the normal direct
    download link on a future run, which the existing logic already handles.
    Returns a human-readable warning string to report, or None if the
    alternate link wasn't present on this card at all (nothing to do)."""
    request_link = card.locator("text=/Submit Intimation Request/i").first
    if await request_link.count() == 0:
        return None

    try:
        step(f"Found 'Submit Intimation Request' link for {filing_type} — clicking")
        await request_link.click()
        await asyncio.sleep(0.5)

        # Confirmed via live error output: the portal templates these modal
        # ids (and their buttons) once PER CARD via *ngFor, so #downldIntimation
        # etc. are duplicated 6x on the page (one per visible filing card,
        # all hidden except the one for whichever card was just clicked).
        # Scoping to `card` (not filed_returns_page) resolves to exactly the
        # one instance nested in this specific card, avoiding a Playwright
        # strict-mode error — which, if left open, blocks all further clicks
        # on the page via its modal backdrop.
        already_raised_modal = card.locator("#downldIntimation")
        confirm_modal = card.locator("#confirmSubIntimation")

        if await already_raised_modal.is_visible(timeout=3000):
            step("'Already raised' modal shown — closing")
            await card.locator("#yesButton").click()
            msg = f"{filing_type} Intimation already requested — not yet available, re-run later to fetch"
            step(f"[Info] {msg}")
            return msg

        if await confirm_modal.is_visible(timeout=3000):
            step("Submit-request confirmation modal shown — clicking Yes")
            await confirm_modal.locator("button.normal-button-primary").first.click()
            await asyncio.sleep(0.5)
            success_modal = card.locator("#downldIntimationFortyEight")
            if await success_modal.is_visible(timeout=5000):
                step("Request-submitted modal shown — closing")
                await card.locator("#yesButton1").click()
            else:
                step("Expected request-submitted modal did not appear (continuing)")
            msg = f"{filing_type} Intimation request placed — check back later to fetch"
            step(f"[Info] {msg}")
            return msg

        msg = f"{filing_type} Intimation request link clicked but outcome unknown"
        step(f"[Warning] {msg}")
        return msg
    except Exception as e:
        msg = f"{filing_type} Intimation request flow failed: {e}"
        step(f"[Warning] {msg}")
        return msg


async def _process_card(card, filing_type: str, filing_date_ddmmyyyy: str, ack_no: str, ay_str: str, prefix: str,
                         pan: str, dob: str, download_dir: str,
                         filed_returns_page: Page, step) -> tuple[list[dict], list[str]]:
    """Downloads Form/Receipt/JSON/Intimation for one already-located,
    already-on-screen filing card. Must be called while `card` is valid on
    the currently rendered page — a card locator from a page that has since
    been navigated away from and back to may point at different content.

    Each filing gets its own subfolder, since one AY can have multiple
    filings (Original, Revised, Rectification, ...): ITR Returns and
    Intimation Orders each get a "{filing_type}-{filing_date_ddmmyyyy}-{ack_no}"
    subfolder so files from different filings never collide or mix. The
    Acknowledgement Number is included as a safety net — confirmed live that
    Filing Type + Filing Date alone can collide (a discarded-then-refiled
    'Original' return sharing both with the return that replaced it)."""
    saved: list[dict] = []
    warns: list[str] = []
    step(f"Processing card: filing_type={filing_type}, filing_date={filing_date_ddmmyyyy}, ack_no={ack_no}")

    migrate_itr_filing_subfolder(download_dir, filing_type, filing_date_ddmmyyyy, ack_no, step)

    filing_subfolder = f"{filing_type}-{filing_date_ddmmyyyy}-{ack_no}" if ack_no else f"{filing_type}-{filing_date_ddmmyyyy}"
    itr_dir = os.path.join(download_dir, "ITR Returns", filing_subfolder)
    intimation_dir = os.path.join(download_dir, "Intimation Orders", filing_subfolder)

    # Confirmed exact button labels/classes: "Download Form" (.dformback —
    # the complete pre-filled ITR form, a PDF rendering of the JSON/XML, NOT
    # an acknowledgement copy), "Download Receipt" (.drecback), "Download
    # JSON" (.dxmlback) — direct buttons on the card, no detail-view expansion.
    #
    # Confirmed live: the SAME "Download Receipt" button/link serves two
    # entirely different documents depending on e-verification status —
    # "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT" once verified, or "INDIAN
    # INCOME TAX RETURN VERIFICATION FORM" (ITR-V) if still pending. Name the
    # saved file accordingly rather than always calling it "Receipt".
    verified = await _is_return_verified(card, step)
    if verified is False:
        receipt_label, receipt_suffix = "ITR-V", "ITR-V"
    else:
        receipt_label, receipt_suffix = "Receipt", "Receipt"

    artifacts = (
        ("Form", ".dformback, button:has-text('Download Form')", f"{prefix}ITR-{ay_str}-{filing_type}-Form.pdf"),
        (receipt_label, ".drecback, button:has-text('Download Receipt')", f"{prefix}ITR-{ay_str}-{filing_type}-{receipt_suffix}.pdf"),
        ("JSON", ".dxmlback, button:has-text('Download JSON')", f"{prefix}ITR-{ay_str}-{filing_type}.json"),
    )
    os.makedirs(itr_dir, exist_ok=True)
    for artifact_label, trigger_selector, filename in artifacts:
        trigger = card.locator(trigger_selector).first
        count = await trigger.count()
        step(f"Looking for {artifact_label} trigger ('{trigger_selector}') — found {count}")
        if count == 0:
            step(f"{artifact_label} button not present on this card — skipping")
            warns.append(f"{filing_type} {artifact_label} not available")
            continue
        output_path = os.path.join(itr_dir, filename)
        ok = await _download_one_file(
            filed_returns_page, trigger, output_path, step,
            f"ITR {filing_type} {artifact_label}", pan=pan, dob=dob,
        )
        if ok:
            saved.append({"filing_type": filing_type, "artifact": artifact_label, "path": output_path, "intimation_date": None})
        else:
            warns.append(f"{filing_type} {artifact_label} failed")

    # Confirmed: intimation is a text link reading exactly "Download
    # Intimation Order Dated Jan 6, 2026" (span.hyperLink) — present only
    # when an intimation exists (absent on e.g. still-"Under Processing"
    # filings). Whether one filing can show more than one is unconfirmed —
    # handled defensively as N links.
    intimation_links = card.locator("text=/Download Intimation Order Dated/i")
    intimation_count = await intimation_links.count()
    step(f"Found {intimation_count} Intimation Order link(s) on this card")
    if intimation_count == 0:
        # No ready intimation — check for the alternate "not generated yet"
        # request-flow link before concluding there's simply none at all.
        request_warning = await _submit_intimation_request(card, filing_type, step)
        if request_warning:
            warns.append(request_warning)
    if intimation_count:
        os.makedirs(intimation_dir, exist_ok=True)
    for j in range(intimation_count):
        link = intimation_links.nth(j)
        try:
            link_text = await link.inner_text()
        except Exception as e:
            step(f"Could not read intimation link #{j} text: {e}")
            link_text = ""
        step(f"Intimation link #{j} text: '{link_text}'")
        date_match = _INTIMATION_DATE_RE.search(link_text)
        raw_date = date_match.group(1) if date_match else ""
        date_str = _sanitize_date_for_filename(raw_date, step)
        filename = f"{prefix}Intimation-{ay_str}-{filing_type}-{date_str}.pdf"
        output_path = os.path.join(intimation_dir, filename)
        ok = await _download_one_file(
            filed_returns_page, link, output_path, step,
            f"Intimation Order ({filing_type})", pan=pan, dob=dob,
        )
        if ok:
            saved.append({"filing_type": filing_type, "artifact": "Intimation", "path": output_path, "intimation_date": date_str})
        else:
            warns.append(f"{filing_type} Intimation Order failed")

    step(f"Card processed: {len(saved)} file(s) saved, {len(warns)} warning(s)")
    return saved, warns


async def navigate_to_view_filed_returns(page: Page, log_callback) -> Page:
    """One-time navigation: e-File > Income Tax Returns > View Filed Returns,
    landing on the rendered filings list. F-14 (multi-year): this is the
    expensive/fragile part (the same e-File hover navigation stabilized
    earlier), so it now runs ONCE per client regardless of how many
    Assessment Years are requested — callers loop `_download_filed_returns_
    for_year()` afterward, which only re-applies the (cheap, in-page) AY
    filter per year."""
    step = make_step_logger(log_callback, "FILEDRET")
    await open_hamburger(page, log_callback, prefix="FILEDRET")
    step("Hamburger menu handled")

    # The ITD portal often leaves a full-screen loading spinner/overlay active
    # for a few seconds which intercepts pointer events. Wait for it to clear.
    step("Waiting for .customLoaderBackdrop to hide (if present)")
    try:
        await page.locator(".customLoaderBackdrop").wait_for(state="hidden", timeout=30000)
        step("Loader backdrop hidden")
    except Exception as e:
        step(f"No loader backdrop detected or wait failed (continuing): {e}")

    await hover_to_income_tax_returns(page, log_callback, prefix="FILEDRET")
    step("Hovered e-File > Income Tax Returns")

    step("Locating 'View Filed Returns' menu item")
    await update_browser_status(page, "Filed Returns: Opening View Filed Returns...")
    # Confirmed exact menu text: "View Filed Returns", under e-File > Income
    # Tax Returns. Confirmed this loads in-page (same e-Filing portal SPA
    # route change) — unlike 26AS/168, it does NOT open a new tab/TRACES.
    view_filed_returns = page.locator("//*[normalize-space(.)='View Filed Returns']").first
    await view_filed_returns.wait_for(state="visible", timeout=30000)
    step("'View Filed Returns' is visible — clicking")
    await view_filed_returns.click()
    filed_returns_page = page
    await filed_returns_page.wait_for_load_state("domcontentloaded", timeout=40000)
    step("View Filed Returns page DOM content loaded")

    await update_browser_status(filed_returns_page, "Filed Returns: Loading filings...")
    # Confirmed: "N Filings till date" heading appears once the list has
    # rendered — wait for it rather than a fixed sleep.
    step("Waiting for 'Filings till date' heading to render")
    await filed_returns_page.locator("text=/Filings till date/i").first.wait_for(
        state="visible", timeout=30000
    )
    step("Filings list heading is visible — list has rendered")
    return filed_returns_page


async def _download_filed_returns_for_year(
    filed_returns_page: Page,
    assessment_year: str,
    download_dir: str,
    log_callback,
    pan: str = "",
    dob: str = "",
    filing_scope: str = "all",
    previous_year: str | None = None,
) -> tuple[bool, str, list[dict]]:
    """Downloads Filed Returns/Intimation Orders for ONE Assessment Year,
    assuming `navigate_to_view_filed_returns()` has already landed on the
    rendered filings list. `previous_year`, if given, is the last AY this
    same page was filtered to (used to uncheck it — see `apply_ay_filter`'s
    docstring)."""
    step = make_step_logger(log_callback, "FILEDRET")
    try:
        step(f"Starting Filed Returns download — AY={assessment_year}, filing_scope={filing_scope}, pan={'set' if pan else 'blank'}")

        os.makedirs(download_dir, exist_ok=True)
        ay_str = assessment_year.replace("-", "_")
        prefix = f"{pan}-" if pan else ""
        migrate_flat_docs_to_subfolders(download_dir, step)

        # Preferred path: filter the list down to just this AY using the
        # confirmed mat-select[formcontrolname='ay'] control — far cheaper
        # than scanning every page. Falls back to the "View More" + page-walk
        # approach if the filter interaction doesn't work as expected (e.g.
        # the apply-button guess inside apply_ay_filter turns out wrong).
        step("Attempting Assessment Year filter (preferred path)")
        filtered = await apply_ay_filter(filed_returns_page, assessment_year, step, previous_year=previous_year)
        step(f"AY filter {'applied' if filtered else 'NOT applied — using fallback scan'}")

        if not filtered:
            # Confirmed exact markup: <div class="viewMoreBtnContainer">
            # <button class="largeButton secondaryButton ...">View more</button>
            # (lowercase "more" — an earlier case-sensitive exact-text XPath
            # match against "View More" silently never matched this button,
            # which is why old-AY lookups previously fell through with only
            # 6 cards ever scanned). Clicking it loads more filings into the
            # paginated list (confirmed it can take multiple clicks — the
            # button keeps reappearing until everything is loaded). This is
            # DISTINCT from the per-card "View more" that expands one
            # filing's own status timeline (a <button class="hyperLink
            # btnstyle" id="stprbtn..."> inside <mat-vertical-stepper>) —
            # the confirmed class selector here can't collide with that.
            page_level_view_more = filed_returns_page.locator(
                ".viewMoreBtnContainer button.largeButton.secondaryButton"
            ).first
            for _ in range(20):  # hard cap so a stuck/relabeled control can't loop forever
                try:
                    if await page_level_view_more.is_visible(timeout=2000):
                        step("Page-level 'View more' found — clicking to load more filings")
                        await page_level_view_more.click()
                        await asyncio.sleep(1.5)
                    else:
                        step("Page-level 'View more' not visible — list fully loaded")
                        break
                except Exception as e:
                    step(f"'View more' check/click failed (continuing): {e}")
                    break

        # Confirmed: the toolbar's "Export to excel" button (button.excelpos)
        # exports whatever is currently shown on screen — since the AY filter
        # was just applied, this export is naturally scoped to just this AY.
        # Only attempted when the filter succeeded; skipped in the fallback
        # page-walk case since the export would then cover ALL years, not
        # just the target one. Saved into ITR Returns/ at the AY level (not
        # a per-filing subfolder) since it's a summary across all filings.
        if filtered:
            step("Attempting Export to Excel (filing-status summary)")
            try:
                export_btn = filed_returns_page.locator(
                    "button.excelpos, button:has-text('Export to excel')"
                ).first
                itr_root = os.path.join(download_dir, "ITR Returns")
                os.makedirs(itr_root, exist_ok=True)
                status_xlsx = os.path.join(itr_root, f"{prefix}ITR-Status-{ay_str}.xlsx")
                async with filed_returns_page.expect_download() as export_dl_info:
                    await export_btn.click()
                await (await export_dl_info.value).save_as(status_xlsx)
                step(f"[Victory] ITR Status Excel saved: {os.path.basename(status_xlsx)}")
            except Exception as e:
                step(f"[Warning] Export to Excel failed (continuing): {e}")
        else:
            step("Skipping Export to Excel — AY filter did not apply, export would span all years")

        # Confirmed card container: <mat-card class="contextBox"> (one per
        # filing). Confirmed AY heading: ".contentHeadingText" reads e.g.
        # " A.Y.  2025-26" (extra whitespace — substring match handles it).
        # Confirmed filing-type value: ".leftSideVal".
        #
        # The results are paginated (6/page, adjustable via an "Items per
        # page" mat-select) with prev/next arrow controls. Confirmed: these
        # arrows are <img alt="next page"> / <img alt="previous page">
        # elements (role="link", not real buttons) — disabled state is
        # aria-disabled="true", not the standard HTML disabled attribute.
        #
        # IMPORTANT: card locators are only valid against whatever page is
        # currently rendered — once the pager advances, a previously-matched
        # `mat-card.contextBox >> nth(i)` locator silently resolves against
        # the NEW page's i-th card instead (Playwright locators re-query
        # lazily, they are not element snapshots). So for filing_scope=="all"
        # we must download from each matching card immediately, before
        # advancing to the next page. For "latest" we only need cheap text
        # (filing type + filing date) recorded per page/index during the
        # scan, then navigate back to the single winning page afterward.
        if filing_scope not in ("all", "latest"):
            step(f"Unrecognized filing_scope '{filing_scope}' — treating as 'all'")

        next_page_btn = filed_returns_page.locator("img[alt='next page']").first
        prev_page_btn = filed_returns_page.locator("img[alt='previous page']").first

        saved_files: list[dict] = []
        warnings: list[str] = []
        found_any_match = False
        candidates: list[dict] = []  # only populated for filing_scope == "latest": {"page", "index", "filing_type", "date_text", "date_parsed"}
        current_page = 0

        for _page_num in range(10):  # hard cap: 10 pages * 6/page covers well over 22 filings
            step(f"Scanning page {current_page} for AY {assessment_year} cards")
            all_cards = filed_returns_page.locator("mat-card.contextBox")
            card_count = await all_cards.count()
            step(f"Page {current_page}: found {card_count} filing card(s)")
            for i in range(card_count):
                card = all_cards.nth(i)
                try:
                    ay_text = await card.locator(".contentHeadingText").first.inner_text()
                except Exception as e:
                    step(f"Card {i}: could not read AY heading ({e})")
                    ay_text = ""
                step(f"Card {i}: AY heading text = '{ay_text.strip()}'")
                if assessment_year not in ay_text:
                    continue
                step(f"Card {i}: MATCHES target AY {assessment_year}")
                found_any_match = True

                try:
                    raw_filing_type = await card.locator(".leftSideVal").first.inner_text()
                except Exception as e:
                    step(f"Card {i}: could not read Filing Type ({e})")
                    raw_filing_type = ""
                filing_type = _sanitize_filing_type(raw_filing_type, step)
                # Always read Filing Date now (not just for "latest") — every
                # filing needs it to build its own subfolder name.
                date_text, date_parsed = await _read_filing_date(card, step)
                filing_date_ddmmyyyy = date_parsed.strftime("%d%m%Y") if date_parsed else "UnknownDate"

                # Confirmed live: a discarded filing is legally treated as
                # never filed — skip it entirely rather than downloading it
                # (and it must never win a "latest" comparison either).
                if await _is_discarded(card, step):
                    step(f"Card {i}: filing is discarded — skipping entirely")
                    warnings.append(f"{filing_type} ({filing_date_ddmmyyyy}) skipped — discarded by taxpayer")
                    continue

                ack_no = await _read_ack_no(card, step)

                if filing_scope == "latest":
                    step(f"Card {i}: recorded as 'latest' candidate — filing_type={filing_type}, date_text='{date_text}'")
                    candidates.append({
                        "page": current_page, "index": i, "filing_type": filing_type,
                        "date_text": date_text, "date_parsed": date_parsed,
                        "filing_date_ddmmyyyy": filing_date_ddmmyyyy, "ack_no": ack_no,
                    })
                else:
                    saved, warns = await _process_card(
                        card, filing_type, filing_date_ddmmyyyy, ack_no, ay_str, prefix, pan, dob,
                        download_dir, filed_returns_page, step
                    )
                    saved_files.extend(saved)
                    warnings.extend(warns)

            try:
                if await _pager_arrow_enabled(next_page_btn, step, "next"):
                    step(f"Advancing from page {current_page} to page {current_page + 1}")
                    await next_page_btn.click()
                    await asyncio.sleep(1)
                    current_page += 1
                else:
                    step("Next-page arrow disabled or absent — reached last page, stopping scan")
                    break
            except Exception as e:
                step(f"Next-page click failed ({e}) — stopping scan")
                break

        if not found_any_match:
            step(f"No cards matched AY {assessment_year} across {current_page + 1} page(s) scanned")
            return False, f"No filed returns found for AY {assessment_year}", []

        if filing_scope == "latest" and candidates:
            # NOT simply the first match: within one AY, cards are listed in
            # filing-chronology order (Original, then any Revised/Defective
            # after it) rather than by date descending — e.g. an AY 2024-25
            # Original filed Oct 11 2024 is listed before its own Revised
            # filed Nov 4 2024, even though the Revised is more recent. So
            # "latest" is resolved by actually comparing parsed Filing Date
            # across candidates, not by list/page position.
            step(f"Resolving 'latest' filing among {len(candidates)} candidate(s) by Filing Date")
            dated = [c for c in candidates if c["date_parsed"] is not None]
            if dated:
                winner = max(dated, key=lambda c: c["date_parsed"])
                step(f"Latest filing selected: page={winner['page']}, index={winner['index']}, filing_type={winner['filing_type']}, date={winner['date_parsed'].date()}")
            else:
                step("Could not parse Filing Date on any candidate — defaulting to first listed filing")
                winner = candidates[0]

            current_page = await _goto_page(winner["page"], current_page, next_page_btn, prev_page_btn, step)
            card = filed_returns_page.locator("mat-card.contextBox").nth(winner["index"])
            saved, warns = await _process_card(
                card, winner["filing_type"], winner["filing_date_ddmmyyyy"], winner["ack_no"], ay_str, prefix, pan, dob,
                download_dir, filed_returns_page, step
            )
            saved_files.extend(saved)
            warnings.extend(warns)

        await update_browser_status(filed_returns_page, "Filed Returns: Download Complete!")
        step(f"Done: {len(saved_files)} file(s) saved, {len(warnings)} warning(s)")

        if not saved_files:
            return False, "All filing downloads failed: " + "; ".join(warnings), []
        if warnings:
            return True, "; ".join(warnings), saved_files
        return True, "", saved_files

    except Exception as e:
        err = str(e)
        step(f"[Error] Failed to download Filed Returns: {err}")
        if "Timeout" in err or "timeout" in err:
            if "e-File" in err or "normalize-space" in err:
                reason = "Timed out — ITD dashboard still loading (try again)"
            else:
                reason = "Timed out waiting for portal response (try again)"
        elif "net::" in err.lower():
            reason = "Network error — check internet connection"
        elif "Target page" in err or "browser has been closed" in err:
            reason = "Browser closed unexpectedly"
        else:
            reason = err[:80] if len(err) <= 80 else err[:77] + "..."
        return False, reason, []


async def download_filed_returns(
    page: Page,
    assessment_years: list[str],
    download_dir_for_year,
    log_callback,
    pan: str = "",
    dob: str = "",
    filing_scope: str = "all",
    on_year_start=None,
) -> dict[str, tuple[bool, str, list[dict], dict]]:
    """F-14 (multi-year) entry point. Navigates to "View Filed Returns" ONCE,
    then for each Assessment Year in `assessment_years`: re-applies the AY
    filter (unchecking whichever year was applied last) and processes that
    year's filings into `download_dir_for_year(assessment_year)`. Returns
    {assessment_year: (ok, msg, saved_files, status_info)} — same per-year
    result shape the single-year version used to return, plus a 4th
    element (F-67 "kill two birds"): `status_info` is whatever
    automation.return_status.scan_latest_status() found for that year,
    reusing the SAME already-navigated-and-AY-filtered page this function
    just used to download documents, rather than a separate full
    navigate+filter+scan later — see that function's own docstring for why
    this is worth doing here instead of only from the dedicated ITR
    Processing Status window. `status_info` is `{"ok": False, ...}` (never
    None) if nothing could be read, so callers can check `.get("ok")`
    uniformly without a None-guard.

    `on_year_start(assessment_year)`, if given, fires right before that
    year's download begins — lets the caller update a per-year "now
    downloading" status instead of only finding out once it's done."""
    from automation.return_status import scan_latest_status

    step = make_step_logger(log_callback, "FILEDRET")
    results: dict[str, tuple[bool, str, list[dict], dict]] = {}
    empty_status = {"ok": False, "reason": "", "status": "", "status_date": "", "filing_date": "", "ack_no": ""}
    try:
        filed_returns_page = await navigate_to_view_filed_returns(page, log_callback)
    except Exception as e:
        step(f"[Error] Could not reach View Filed Returns: {e}")
        for ay in assessment_years:
            results[ay] = (False, f"Navigation failed: {e}", [], dict(empty_status))
        return results

    previous_year: str | None = None
    for assessment_year in assessment_years:
        if on_year_start:
            on_year_start(assessment_year)
        download_dir = download_dir_for_year(assessment_year)
        dl_ok, dl_msg, dl_saved = await _download_filed_returns_for_year(
            filed_returns_page, assessment_year, download_dir, log_callback,
            pan=pan, dob=dob, filing_scope=filing_scope, previous_year=previous_year,
        )
        try:
            status_info = await scan_latest_status(filed_returns_page, assessment_year, step)
        except Exception as e:
            step(f"[Warning] Could not read processing status for AY {assessment_year} (continuing): {e}")
            status_info = dict(empty_status)
        results[assessment_year] = (dl_ok, dl_msg, dl_saved, status_info)
        previous_year = assessment_year
    return results
