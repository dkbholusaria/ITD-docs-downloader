"""
automation/batch_handlers.py
=============================
Per-document-type batch handlers, extracted from app.py's _execute_batch
dispatch chain. Each handler downloads exactly one document type for one
already-logged-in client — across every selected year in one call — and
reports progress via the passed-in callbacks. No dependency on the App
class, so adding a new document type here doesn't grow app.py further.

F-14 (multi-year): every handler now takes `year_specs` (a list of
per-year dicts, see below) instead of a single `ay`/`fy`, and an
`out_for_year(year_type, value) -> str` callable instead of a single `out`
path. Each handler is responsible for looping its own years internally,
navigating to its target page/portal ONCE per client and reselecting the
year via whatever lightweight in-page control that doc type already
exposes (a dropdown or filter) — not by repeating the full top-level e-File
navigation once per year. See PlansofThisProject/F-14_multi_year_download.md
for the full design writeup.

`year_specs` entries: {"ay_label": str, "value": str, "fy": str,
"year_type": "AY"|"TY", "form_type": "26AS"|"168"}.

`set_status(pan, ay_label, text)` — every status update is now tied to a
specific year, so the progress dialog (one row per (pan, ay_label) pair)
and vault history stay correctly attributed.

Every handler shares the same signature (even though each only uses a
subset of the parameters) so the dispatch loop in app.py can call any of
them uniformly via the HANDLERS table.
"""
import asyncio

from automation.downloader_ais_tis import run_request_ais, run_download_ais_tis
from automation.downloader_filed_returns import download_filed_returns
from automation.downloader_challans import download_challans
from forms import form_spec, DEFAULT_FORM

DOC_TYPE_LABELS = {
    "26as": "26AS/Form 168",
    "request_ais": "AIS Request",
    "ais_tis": "AIS/TIS",
    "filed_returns": "Filed Returns",
    "challans": "Tax Challans",
}


# Confirmed live via log: the dashboard's own e-File > "Income Tax Returns"
# flow lands here after login ("[Auth] Dashboard ready: .../#/dashboard/
# fileIncomeTaxReturn"). Used to force the SPA's client-side router back to
# the base dashboard view between handlers.
_DASHBOARD_HASH = "#/dashboard/fileIncomeTaxReturn"


async def ensure_dashboard(page, log_callback=None):
    """Reset the shared tab back to the base dashboard route before the next
    e-File-based handler starts its own navigation. See git history for the
    full investigation — this alone did not fix the "Income Tax Returns"
    hover timeout (that needed the click-vs-hover fix plus a retry loop),
    but it's kept as a harmless, cheap reset between handlers."""
    def _log(msg):
        if log_callback:
            log_callback(msg)
    try:
        await page.evaluate(f"window.location.hash = '{_DASHBOARD_HASH}'")
        await asyncio.sleep(1)
        try:
            await page.locator(".customLoaderBackdrop").wait_for(state="hidden", timeout=15000)
        except Exception:
            pass
        await page.keyboard.press("Escape")
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        _log(f"[NAV-RESET] Routed back to dashboard hash before handler navigation (url: {page.url})")
    except Exception as e:
        _log(f"[NAV-RESET] Reset attempt failed (continuing): {e}")


async def handle_26as(page, pan, dob, year_specs, out_for_year, log_callback, set_status,
                       filing_scope="all", is_running=None) -> dict:
    """26AS (AY years) and Form 168 (TY years) are different destination
    apps (TRACES 1.0 vs TRACES 2.0) and cannot share one open-once session —
    so this partitions year_specs by form_type and calls each downloader's
    multi-year entry point at most once per group (0, 1, or 2 navigations
    total, never once per year)."""
    await ensure_dashboard(page, log_callback)

    ay_label_by_value = {s["value"]: s["ay_label"] for s in year_specs}
    ay_specs = [s for s in year_specs if s.get("form_type", DEFAULT_FORM) != "168"]
    ty_specs = [s for s in year_specs if s.get("form_type") == "168"]

    txt_paths = {}   # ay_label -> txt_path (for the 26AS→Excel conversion step)
    form_labels = {}

    for group_specs, form_code in ((ay_specs, "26AS"), (ty_specs, "168")):
        if not group_specs:
            continue
        _spec = form_spec(form_code)
        _form = _spec["label"]
        year_type = group_specs[0]["year_type"]
        values = [s["value"] for s in group_specs]
        value_to_ay_label = {s["value"]: s["ay_label"] for s in group_specs}
        for s in group_specs:
            set_status(pan, s["ay_label"], f"⏳ Queued — {_form}")

        def _out_for_value(value, _yt=year_type):
            return out_for_year(_yt, value)

        def _on_year_start(value, _map=value_to_ay_label, _form=_form):
            set_status(pan, _map.get(value, value), f"⏳ Downloading {_form}...")

        results = await _spec["download"](page, values, _out_for_value, log_callback, pan=pan, dob=dob,
                                           on_year_start=_on_year_start)

        for value, (ok, err_msg, txt_path) in results.items():
            ay_label = ay_label_by_value.get(value, value)
            if not ok:
                set_status(pan, ay_label, f"❌ {_form} Failed — {err_msg}")
                continue
            if err_msg:
                set_status(pan, ay_label, f"⚠ Partially Completed — {err_msg}")
            else:
                set_status(pan, ay_label, f"✅ {_form} Downloaded")
            if txt_path:
                txt_paths[ay_label] = txt_path
                form_labels[ay_label] = _form

    # Convert each year's 26AS/168 TXT to Excel immediately, one at a time.
    for ay_label, txt_path in txt_paths.items():
        _form = form_labels.get(ay_label, "26AS")
        set_status(pan, ay_label, "⏳ Converting to Excel...")
        try:
            from automation.as26_converter import convert_26as_txt
            log_callback(f"[Convert] Converting {_form} → Excel/HTML for {pan} ({ay_label})…")
            convert_26as_txt(txt_path, log_callback=log_callback)
            set_status(pan, ay_label, f"✅ {_form} + Excel + HTML")
        except Exception as _conv_exc:
            log_callback(f"[Convert] Warning: conversion failed for {pan} ({ay_label}): {_conv_exc}")
            set_status(pan, ay_label, "⚠ Excel convert failed")

    return {"txt_paths": txt_paths}


async def handle_request_ais(page, pan, dob, year_specs, out_for_year, log_callback, set_status,
                              filing_scope="all", is_running=None) -> dict:
    await ensure_dashboard(page, log_callback)
    fiscal_years = [s["fy"] for s in year_specs]
    fy_to_ay_label = {s["fy"]: s["ay_label"] for s in year_specs}

    for s in year_specs:
        set_status(pan, s["ay_label"], "⏳ Queued — AIS + TIS")

    def _out_for_fy(fy):
        # AIS/TIS folders are keyed by FY, always under the "AY_"-style
        # prefix matching whichever year_spec this FY belongs to.
        spec = next((s for s in year_specs if s["fy"] == fy), None)
        yt = spec["year_type"] if spec else "AY"
        value = spec["value"] if spec else fy
        return out_for_year(yt, value)

    def _on_year_start(fy):
        set_status(pan, fy_to_ay_label.get(fy, fy), "⏳ Requesting AIS + TIS...")

    results = await run_request_ais(page, fiscal_years, _out_for_fy, log_callback,
                                     pan=pan, dob=dob,
                                     status_callback=lambda t, _p=pan: log_callback(f"[AIS-Request] {t}"),
                                     on_year_start=_on_year_start)
    ais_statuses = {}
    ref_ids = {}
    for fy, outcome in results.items():
        ay_label = fy_to_ay_label.get(fy, fy)
        ais_status = outcome.get("status")
        ref = outcome.get("ref_id", "")
        ais_statuses[ay_label] = ais_status
        if ref:
            ref_ids[ay_label] = ref
            log_callback(f"[AIS] Generation queued for {ay_label} — Ref ID: {ref}")
        tis_outcome = outcome.get("tis")
        # Reuse the existing combined-label helper for a consistent status string.
        from automation.downloader_ais_tis import combined_status_label
        set_status(pan, ay_label, combined_status_label(outcome, tis_outcome))

    return {"ais_statuses": ais_statuses, "ref_ids": ref_ids}


async def handle_ais_tis(page, pan, dob, year_specs, out_for_year, log_callback, set_status,
                          filing_scope="all", is_running=None) -> dict:
    # "Download Previously Requested AIS" — fetch ONLY the AIS PDF from
    # Activity History. TIS is not re-downloaded here (it was already
    # grabbed during the Request step).
    await ensure_dashboard(page, log_callback)
    fiscal_years = [s["fy"] for s in year_specs]
    fy_to_ay_label = {s["fy"]: s["ay_label"] for s in year_specs}

    for s in year_specs:
        set_status(pan, s["ay_label"], "⏳ Queued — AIS from Activity History")

    def _out_for_fy(fy):
        spec = next((s for s in year_specs if s["fy"] == fy), None)
        yt = spec["year_type"] if spec else "AY"
        value = spec["value"] if spec else fy
        return out_for_year(yt, value)

    def _on_year_start(fy):
        set_status(pan, fy_to_ay_label.get(fy, fy), "⏳ Downloading AIS from Activity History...")

    results = await run_download_ais_tis(
        page, fiscal_years, _out_for_fy, log_callback, pan=pan, dob=dob,
        dl_ais=True, dl_tis=False,
        should_continue=(is_running if is_running else (lambda: True)),
        status_callback=lambda t: log_callback(f"[AIS-Download] {t}"),
        on_year_start=_on_year_start)

    from automation.downloader_ais_tis import combined_status_label
    for fy, outcome in results.items():
        ay_label = fy_to_ay_label.get(fy, fy)
        set_status(pan, ay_label, combined_status_label(outcome.get("ais"), outcome.get("tis")))
    return {}


async def handle_filed_returns(page, pan, dob, year_specs, out_for_year, log_callback, set_status,
                                filing_scope="all", is_running=None) -> dict:
    await ensure_dashboard(page, log_callback)
    ay_label_by_value = {s["value"]: s["ay_label"] for s in year_specs}
    values = [s["value"] for s in year_specs]

    for s in year_specs:
        set_status(pan, s["ay_label"], "⏳ Queued — Filed Returns")

    def _out_for_value(value):
        spec = next((s for s in year_specs if s["value"] == value), None)
        yt = spec["year_type"] if spec else "AY"
        return out_for_year(yt, value)

    def _on_year_start(value):
        set_status(pan, ay_label_by_value.get(value, value), "⏳ Downloading Filed Returns...")

    results = await download_filed_returns(
        page, values, _out_for_value, log_callback, pan=pan, dob=dob, filing_scope=filing_scope,
        on_year_start=_on_year_start,
    )

    all_saved = []
    # F-67 "kill two birds": download_filed_returns() now also reads each
    # year's current processing status while it's already there for the
    # document download — collected here (keyed by the real AY value, same
    # key vault.record_return_status() already uses) for the caller to
    # persist, instead of needing a separate Check Processing Status run.
    return_statuses = {}
    for value, (fr_ok, fr_msg, fr_saved, fr_status) in results.items():
        ay_label = ay_label_by_value.get(value, value)
        all_saved.extend(fr_saved)
        if fr_status.get("ok"):
            return_statuses[value] = fr_status
        if fr_ok:
            if fr_msg:
                set_status(pan, ay_label, f"⚠ Partially Completed — {fr_msg}")
            else:
                set_status(pan, ay_label, f"✅ Filed Returns Downloaded ({len(fr_saved)} file(s))")
        else:
            set_status(pan, ay_label, f"❌ Filed Returns Failed — {fr_msg}")
    return {"saved": all_saved, "return_statuses": return_statuses}


async def handle_challans(page, pan, dob, year_specs, out_for_year, log_callback, set_status,
                           filing_scope="all", is_running=None) -> dict:
    """e-Pay Tax's Payment History is scoped to ONE Income-tax Act per
    navigation (1961 for AY years, 2025 for TY years) — same reason 26AS
    and Form 168 can't share one open-once session — so this partitions
    year_specs by year_type and calls download_challans() at most once per
    group (0, 1, or 2 navigations total, never once per year)."""
    await ensure_dashboard(page, log_callback)

    ay_specs = [s for s in year_specs if s["year_type"] != "TY"]
    ty_specs = [s for s in year_specs if s["year_type"] == "TY"]

    all_saved = []
    for group_specs, group_year_type in ((ay_specs, "AY"), (ty_specs, "TY")):
        if not group_specs:
            continue
        value_to_ay_label = {s["value"]: s["ay_label"] for s in group_specs}
        values = [s["value"] for s in group_specs]
        for s in group_specs:
            set_status(pan, s["ay_label"], "⏳ Queued — Tax Challans")

        def _out_for_value(value, _yt=group_year_type):
            return out_for_year(_yt, value)

        def _on_year_start(value, _map=value_to_ay_label):
            set_status(pan, _map.get(value, value), "⏳ Downloading Tax Challans...")

        results = await download_challans(
            page, values, _out_for_value, log_callback, pan=pan, dob=dob,
            year_type=group_year_type, on_year_start=_on_year_start,
        )

        for value, (ok, msg, saved) in results.items():
            ay_label = value_to_ay_label.get(value, value)
            all_saved.extend(saved)
            if ok:
                if msg:
                    set_status(pan, ay_label, f"⚠ Partially Completed — {msg}")
                else:
                    set_status(pan, ay_label, f"✅ Challans Downloaded ({len(saved)} file(s))")
            else:
                set_status(pan, ay_label, f"❌ Challans Failed — {msg}")

    return {"saved": all_saved}


HANDLERS = {
    "26as": handle_26as,
    "request_ais": handle_request_ais,
    "ais_tis": handle_ais_tis,
    "filed_returns": handle_filed_returns,
    "challans": handle_challans,
}

# Filed Returns has only ever been tested (and confirmed working) running
# FIRST, alone. It reliably fails when it runs after 26AS's e-File hover
# has already been used on the same tab; the reverse combination (Filed
# Returns first, then 26AS) has never been tried. Reload/new-tab resets are
# both ruled out (they log the session out entirely — confirmed live), so
# as a cheap, low-risk experiment, always dispatch Filed Returns before the
# other e-File-based handlers; AIS handlers don't use the e-File menu at
# all, so their position doesn't matter for this bug. Challans (F-61) is
# also e-File-based (e-Pay Tax hangs directly off the same menu) but is
# brand new and untested in multi-select batches — placed last among the
# e-File handlers so any second/third-handler fragility hits the newest,
# least-trusted code first rather than regressing the two already-proven
# handlers; needs live multi-select testing (e.g. Filed Returns + Challans,
# 26AS + Challans) before this ordering can be trusted.
HANDLER_ORDER = ["filed_returns", "26as", "challans", "request_ais", "ais_tis"]


def ordered_doc_types(selected_docs):
    """selected_docs is a set with unpredictable iteration order; return it
    as a list following HANDLER_ORDER so dispatch order is deterministic."""
    return [d for d in HANDLER_ORDER if d in selected_docs] + \
           [d for d in selected_docs if d not in HANDLER_ORDER]
