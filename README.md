# AayDocCapio

**v2.4.1** — A secure, standalone desktop utility for **bulk downloading Form 26AS, Form 168, AIS, TIS, and Filed Returns & Intimation Orders**, and **generating tax payment challans**, from the [Income Tax Department e-Filing portal](https://eportal.incometax.gov.in) for multiple clients — across multiple Assessment/Tax Years — in one click, and **emailing those documents directly to clients**.

Built with **PyQt6** + **Playwright**. Runs on Windows, macOS, and Linux/WSL.

---

## What's New in 2.4.1

- **ITR Processing Status Tracker** — log in and see where each client's return actually stands (Pending e-Verification, Processed, Processed with Refund/Demand Due, etc.), shown exactly as the portal shows it. A new "Return Status" window lets you filter by client and Year and re-check live; ordinary Downloads runs now keep this data fresh automatically too.
- **Client Groups** — organize clients into groups (e.g. by family or firm), with a two-panel Manage Groups window to add/remove members, group filters in the main grid and Return Status window, and Group support in the Client Master import template.
- **A branded splash screen** on startup.

See the full [CHANGELOG](CHANGELOG.md) for details.

---

## Quick Links

- [Full Documentation](Documentation/README.md)
- [Windows Build Guide](Documentation/windows_build.md) — Nuitka, Inno Setup, WiX MSI
- [macOS Support](Documentation/macos_support.md) — setup, what changed, app-bundle build
- [Development Log](Documentation/DEVELOPMENT_LOG.md) — design decisions & debugging history
- [Changelog](CHANGELOG.md)

---

## Quick Start

```bash
git clone https://github.com/dkbholusaria/AayDocCapio.git
cd AayDocCapio
pip install -r requirements.txt
playwright install chromium
python app.py
```

> **Requires Google Chrome** for AIS/TIS downloads (the app launches real Chrome
> via `channel="chrome"`). 26AS works without it.

On macOS you can also run `bash scripts/setup.sh` once and then double-click
`scripts/AayDocCapio.command` in Finder — see the [macOS guide](Documentation/macos_support.md).

---

## Features

| Feature | Details |
|---|---|
| Form 26AS | PDF + TXT download for all selected clients |
| AIS / TIS | Request generation + download once ready |
| **Email delivery** | **Batch email tax docs to clients with one click** |
| **Provider presets** | **Gmail, Outlook, Office 365, Exchange, Yahoo, iCloud, Custom** |
| **Rich text composer** | **Font, size, B/I/U, placeholders, CC, BCC** |
| Managing Clients | Encrypted credentials (AES-256 via Fernet) |
| Bulk import | Excel / CSV template with Name, PAN, DOB, Password, Email, CC |
| Assessment Year management | Toggle years on/off, add custom AY/TY entries |
| Download history | Last status and folder per client per AY, shown in table |
| Themes | Light and Dark Navy; extensible via `themes.py` |
| Resume after Stop | Retry only the clients that were not completed |
| Excel report | Per-client status, folder hyperlink, per-row timestamp |
| Headless mode | Run browser hidden or visible (for CAPTCHA handling) |

---

## Architecture

```
app.py                   — Main window and all application logic
config.py                — Path helpers (_app_dir, _bundled_dir, etc.)
utils.py                 — Shared utilities (timestamps, etc.)
themes.py                — ThemeColors dataclass, THEMES registry, build_stylesheet()
vault.py                 — Encrypted client vault, settings, download history
ui/
  _theme.py              — Shared active-theme state (avoids circular imports)
  helpers.py             — Widget factory helpers (_btn, _lbl, _shadow, _status_style)
  widgets.py             — StyledComboBox and delegates
  dialogs.py             — ManageYearsDialog, BatchProgressDialog, MailDocsDialog, SmtpSettingsDialog
automation/
  browser.py             — Playwright browser launch / auth helpers
  downloader_26as.py     — 26AS download + TXT → Excel/HTML converter
  downloader_ais_tis.py  — AIS/TIS download
  emailer.py             — SMTP send, batch send, session logging, document list builder
  as26_converter.py      — 26AS TXT parser
  errors.py              — Human-readable error messages
scripts/
  release.sh             — GitHub release automation (--dry-run, --rerun)
  bump.sh                — Version bump utility
  AayDocCapio.command    — macOS double-click launcher
resources/icons/         — All PNG icons (buttons, menus, email providers)
assessment_years.json    — AY/TY list (editable via Settings → Manage Years)
```

---

## Requirements

- Python 3.11+
- Google Chrome (for AIS/TIS; Chromium suffices for 26AS)
- See `requirements.txt` for Python packages

---

## Support

Use **Help -> Report Bug / Request Feature...** inside the app to open a
structured GitHub form for bug reports or feature requests.

---

## License

AayDocCapio is licensed under the GNU Affero General Public License v3.0 or
later (`AGPL-3.0-or-later`) for changes and distributions after the
`old-license-mit` tag. See [LICENSE](LICENSE).

Releases and commits up to and including `old-license-mit` remain available
under the MIT License. See [legacy MIT notice](Documentation/legacy-mit-notice.md).

New source files should include `Copyright (C) 2026 Deepak Bholusaria` and
`SPDX-License-Identifier: AGPL-3.0-or-later` near the top of the file.
