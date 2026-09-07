"""
AayDocCapio — ITD Bulk Document Downloader (Form 26AS, AIS, TIS)
Run:  python3 app.py
"""
from version import __version__ as APP_VERSION

import sys, os, json, asyncio, threading, datetime, logging
from urllib.parse import urlencode

# Force XCB (X11) backend on Linux/WSL2 — must be set before Qt initialises.
# WSLg sets WAYLAND_DISPLAY which Qt6 prefers; unsetting it prevents the
# "Wayland connection broke" hang that requires WSL restart to fix.
if sys.platform != "win32":
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ.pop("WAYLAND_DISPLAY", None)
from themes import THEMES, ThemeColors, build_stylesheet, get_theme, MONO_FONT_NAME as _MONO_FONT
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
    QLineEdit, QCheckBox, QComboBox, QFileDialog,
    QHBoxLayout, QVBoxLayout,
    QMessageBox, QTextEdit, QDialog, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QToolButton, QMenu, QCalendarWidget, QSystemTrayIcon,
    QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, pyqtSlot, QTimer, QMetaObject, Q_ARG, QUrl,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import QFont, QTextCursor, QColor, QRegularExpressionValidator, QPalette, QAction, QIcon, QPixmap, QDesktopServices
from PyQt6.QtCore import QRegularExpression

from config import _app_dir, _default_download_dir, _bundled_dir
from utils import get_timestamp, notify_windows
from ui._theme import _t
from ui.helpers import _btn, _lbl, _shadow
from ui.widgets import StyledComboBox, CheckableComboBox
from ui.dialogs import (
    ManageYearsDialog, BatchProgressDialog, DownloadPickerDialog,
    GenerateChallansDialog, ChallanGenerationProgressDialog,
    ReturnStatusDialog, ReturnStatusProgressDialog,
)
from ui.log_history import LogHistoryDialog, LogStore
from automation.errors import _friendly_error

sys.path.insert(0, _bundled_dir())

try:
    from vault import VaultManager
    from automation.browser import browser_manager
    from automation.auth import login_itd, logout_itd
    from automation.batch_handlers import HANDLERS, DOC_TYPE_LABELS, ordered_doc_types
    from forms import form_for, DEFAULT_FORM
except Exception as _import_err:
    import traceback
    _msg = (
        f"Failed to load required modules.\n\n"
        f"{traceback.format_exc()}\n\n"
        f"bundled_dir: {_bundled_dir()}\n"
        f"sys.path: {sys.path[:5]}"
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, _msg, "AayDocCapio — Startup Error", 0x10)
    except Exception:
        pass
    sys.exit(1)



# ── Client Picker Dialog ──────────────────────────────────────────────────────
class _ClientPickerDialog(QDialog):
    """Reusable popup for selecting one or more clients before Edit / Delete.

    mode='edit'   — single-select
    mode='delete' — multi-select with table layout and selected count
    """

    def __init__(self, parent, clients, mode):
        super().__init__(parent)
        self._mode    = mode
        self._clients = clients          # list of {id, name, pan}

        t = _t()
        self.setWindowTitle("Select Client to Edit" if mode == "edit"
                            else "Select Client(s) to Delete")
        self.setMinimumWidth(560)
        self.setMinimumHeight(460)
        self.resize(620, 520)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(
            f"QDialog{{background:{t.bg_window};}}"
            f"QLabel{{background:transparent;border:none;color:{t.text_primary};font-size:12px;}}"
            f"QLineEdit{{border:1px solid {t.border};border-radius:6px;padding:5px 10px;"
            f"font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
            f"QTableWidget{{border:1px solid {t.border};border-radius:6px;"
            f"background:{t.bg_table};gridline-color:{t.grid};outline:0;}}"
            f"QTableWidget::item{{padding:4px 8px;border:none;}}"
            f"QTableWidget::item:selected{{background:{t.accent};color:white;}}"
            f"QHeaderView::section{{background:{t.bg_header};color:{t.text_muted};"
            f"border:none;border-right:1px solid {t.border};"
            f"border-bottom:1px solid {t.border};font-weight:bold;"
            f"font-size:11px;height:30px;padding:0 8px;}}"
            f"QCheckBox::indicator{{width:14px;height:14px;border:1.5px solid {t.border};"
            f"border-radius:3px;background:{t.bg_checkbox};}}"
            f"QCheckBox::indicator:checked{{background:{t.accent};border-color:{t.accent};}}"
        )

        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 14, 16, 14)
        vl.setSpacing(8)

        # Header row: subtitle + selected count
        hdr_row = QHBoxLayout()
        sub = "Select one client to edit:" if mode == "edit" else "Select clients to delete:"
        sub_lbl = QLabel(sub)
        sub_lbl.setStyleSheet(f"color:{t.text_muted};font-size:11px;")
        hdr_row.addWidget(sub_lbl)
        hdr_row.addStretch()
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color:{t.accent};font-size:11px;font-weight:bold;")
        hdr_row.addWidget(self._count_lbl)
        vl.addLayout(hdr_row)

        # Search box
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name or PAN…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        vl.addWidget(self._search)

        # Table
        self._table = QTableWidget(len(clients), 3)
        self._table.setHorizontalHeaderLabels(["", "Name", "PAN"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setSectionsClickable(True)
        self._table.setSortingEnabled(True)
        self._table.setColumnWidth(0, 36)
        self._table.setColumnWidth(1, 280)
        self._table.setColumnWidth(2, 120)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            f"QTableWidget{{alternate-background-color:{t.bg_table_alt};}}")

        self._checks = []   # (client_id, QCheckBox)
        for row, c in enumerate(clients):
            self._table.setRowHeight(row, 34)

            cb_widget = QWidget()
            cb_widget.setStyleSheet("background:transparent;")
            cb_lay = QHBoxLayout(cb_widget)
            cb_lay.setContentsMargins(0, 0, 0, 0)
            cb_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb = QCheckBox()
            cb.stateChanged.connect(self._on_check_changed)
            cb_lay.addWidget(cb)
            self._table.setCellWidget(row, 0, cb_widget)
            self._checks.append((c["id"], cb))

            name_item = QTableWidgetItem(c["name"])
            name_item.setForeground(QColor(t.text_primary))
            self._table.setItem(row, 1, name_item)

            pan_item = QTableWidgetItem(c["pan"])
            pan_item.setForeground(QColor(t.text_muted))
            pan_item.setFont(QFont(_MONO_FONT, 10))
            pan_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, 2, pan_item)

        # Click on row toggles checkbox
        self._table.cellClicked.connect(self._on_cell_clicked)
        vl.addWidget(self._table, stretch=1)

        # Footer
        from ui.helpers import _btn as _hbtn
        btn_row = QHBoxLayout()
        if mode == "delete":
            sel_all_btn = _hbtn("Select All", "outline", height=30, icon="btn_select_all.png")
            sel_all_btn.clicked.connect(self._select_all)
            btn_row.addWidget(sel_all_btn)
            sel_none_btn = _hbtn("Select None", "outline", height=30, icon="btn_select_none.png")
            sel_none_btn.clicked.connect(self._select_none)
            btn_row.addWidget(sel_none_btn)
        btn_row.addStretch()
        cancel_btn = _hbtn("Cancel", "secondary", height=32, icon="btn_cancel.png")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(8)
        ok_style = "delete" if mode == "delete" else "primary"
        ok_icon  = "btn_delete.png" if mode == "delete" else "icon_edit.png"
        self._ok_btn = _hbtn("Edit" if mode == "edit" else "Delete", ok_style, height=32, icon=ok_icon)
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)
        vl.addLayout(btn_row)

    def _on_cell_clicked(self, row, col):
        _, cb = self._checks[row]
        cb.setChecked(not cb.isChecked())

    def _select_all(self):
        for row, (_, cb) in enumerate(self._checks):
            if not self._table.isRowHidden(row):
                cb.setChecked(True)

    def _select_none(self):
        for _, cb in self._checks:
            cb.setChecked(False)

    def _on_search(self, text):
        q = text.strip().lower()
        for row, (c, (_, cb)) in enumerate(zip(self._clients, self._checks)):
            match = not q or q in c["name"].lower() or q in c["pan"].lower()
            self._table.setRowHidden(row, not match)
            if not match:
                cb.setChecked(False)

    def _on_check_changed(self, _state):
        if self._mode == "edit":
            sender = self.sender()
            if sender and sender.isChecked():
                for _, cb in self._checks:
                    if cb is not sender:
                        cb.blockSignals(True)
                        cb.setChecked(False)
                        cb.blockSignals(False)
        n = sum(1 for _, cb in self._checks if cb.isChecked())
        self._ok_btn.setEnabled(n == 1 if self._mode == "edit" else n >= 1)
        if self._mode == "delete":
            self._count_lbl.setText(f"{n} selected" if n else "")

    @property
    def selected_ids(self):
        return [id_ for id_, cb in self._checks if cb.isChecked()]


# ── Main Window ───────────────────────────────────────────────────────────────
class AayDocCapioApp(QMainWindow):
    _log_signal = pyqtSignal(str)
    _batch_done_signal = pyqtSignal()
    _show_progress_signal = pyqtSignal(list, object, list, str, str)   # (targets, selected_docs: set, year_specs, output_dir, year_tag)
    _challan_done_signal = pyqtSignal()
    _show_challan_progress_signal = pyqtSignal(list, str, str, str)   # (targets, fy_value, type_label, output_dir)
    _return_status_done_signal = pyqtSignal()
    _show_return_status_progress_signal = pyqtSignal(list, str)   # (targets, ay_value)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AayDocCapio — Tax Documents. Delivered to You.")
        self.setMinimumSize(1100, 720)
        self.resize(1200, 780)
        from automation.emailer import log_session_start
        log_session_start()

        self.vault = VaultManager(
            vault_path=os.path.join(_app_dir(), "tax_vault.json"))
        self.log_store = LogStore()
        self.selected_ids = set()
        self.editing_id = None
        self.is_running = False
        self._checkbox_map = {}
        self._id_to_row: dict = {}
        
        # Generate checkmark image for custom check box styling
        self.checkmark_path = os.path.join(_app_dir(), "checkmark.png")
        if not os.path.exists(self.checkmark_path):
            try:
                from PIL import Image, ImageDraw
                img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.line([(3, 8), (7, 12), (13, 4)], fill=(255, 255, 255, 255), width=2)
                img.save(self.checkmark_path, "PNG")
            except Exception as e:
                print(f"Error generating checkmark: {e}")

        self._ais_requested_time = None   # datetime when Request AIS last completed
        self._last_selected_docs = set()  # selected_docs of last completed batch
        self._ais_results = {}            # pan → "instant" | "queued" | "failed" | "skipped"
        self._last_errors = {}            # pan → error message string
        self._batch_loop = None           # asyncio event loop for the running batch
        self._batch_task = None           # asyncio Task for the running batch
        self._batch_aborted = False       # True if user clicked Stop
        self._skip_current  = False       # True when user clicks Skip for current client
        self._last_batch_params = None    # (year_specs, root_dir, selected_docs) for resume

        self._log_signal.connect(self._append_log)
        self._batch_done_signal.connect(self._on_batch_done)
        self._show_progress_signal.connect(self._show_progress_dialog)
        self._progress_dialog = None   # BatchProgressDialog instance

        # F-64: separate running/progress state from the download batch above
        # — a challan-generation run and a download run are different
        # in-flight flows, each with their own is_running-style guard.
        self._challan_running = False
        self._challan_loop = None
        self._challan_task = None
        self._challan_aborted = False
        self._challan_progress_dialog = None
        self._challan_done_signal.connect(self._on_challan_batch_done)
        self._show_challan_progress_signal.connect(self._show_challan_progress_dialog)

        self._retstatus_running = False
        self._retstatus_loop = None
        self._retstatus_task = None
        self._retstatus_aborted = False
        self._retstatus_progress_dialog = None
        self._retstatus_on_done = None
        self._return_status_done_signal.connect(self._on_return_status_batch_done)
        self._show_return_status_progress_signal.connect(self._show_return_status_progress_dialog)

        try:
            log_path = os.path.join(_app_dir(), "app.log")
            _MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
            if os.path.exists(log_path) and os.path.getsize(log_path) > _MAX_LOG_BYTES:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(content[len(content) // 2:])
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== Session Started {get_timestamp()} ===\n")
            # Route stdlib logging (used by config._open_path etc.) to app.log
            _fh = logging.FileHandler(log_path, encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                               datefmt="%d-%m-%Y %H:%M:%S"))
            logging.getLogger().addHandler(_fh)
            logging.getLogger().setLevel(logging.INFO)
        except Exception:
            pass

        self._current_theme = self.vault.get_setting("theme", "light")
        self._build_ui()
        self.refresh_grid()
        # Apply saved theme after UI is built
        QTimer.singleShot(0, lambda: self._apply_theme(self._current_theme))
        self._setup_tray()

        client_count = len(self.vault.get_all_assessees())
        self.log(f"[System] AayDocCapio v{APP_VERSION} started — {client_count} client(s) in vault.")

        # Check Chromium on startup in background — installs silently if missing
        QTimer.singleShot(1500, self._check_browser)

        # Check for app updates 3s after startup
        QTimer.singleShot(3000, self._check_for_update)

    # ── System Tray ───────────────────────────────────────────────────────────

    def _setup_tray(self):
        """Create a persistent system-tray icon (always present, used during batch)."""
        # Prefer .ico on Windows — it contains all sizes (16/24/32/48…256px) for
        # pixel-perfect rendering at every DPI. Fall back to PNG on other platforms.
        res = os.path.join(_bundled_dir(), "resources")
        ico = os.path.join(res, "app_icon.ico")
        png = os.path.join(res, "app_icon.png")
        icon_path = ico if (sys.platform == "win32" and os.path.isfile(ico)) else png
        tray_icon = QIcon(icon_path) if os.path.isfile(icon_path) else self.windowIcon()

        self._tray = QSystemTrayIcon(tray_icon, self)
        self._tray.setToolTip("AayDocCapio")

        menu = QMenu()
        self._tray_restore_act = QAction("Restore", self)
        self._tray_restore_act.triggered.connect(self._tray_restore)
        self._tray_send_act = QAction("Send to Tray", self)
        self._tray_send_act.triggered.connect(self._tray_to_system_manual)
        self._tray_send_act.setVisible(False)
        self._tray_stop_act = QAction("Stop Batch", self)
        self._tray_stop_act.triggered.connect(self._tray_stop_active)
        self._tray_stop_act.setEnabled(False)
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(self._tray_restore_act)
        menu.addAction(self._tray_send_act)
        menu.addAction(self._tray_stop_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_tray_activated)
        # Don't show() the tray icon until we actually send to tray

    def _tray_to_system(self, n_clients: int):
        """Auto-hide to tray at batch start (called with client count)."""
        self._tray.setToolTip(f"AayDocCapio — Running ({n_clients} client(s)…)")
        self._tray_stop_act.setEnabled(True)
        self._tray_send_act.setVisible(False)  # hidden while already in tray
        self._tray.show()
        if self._progress_dialog:
            self._progress_dialog.hide()
        self.hide()
        self._tray_show_hint()

    def _tray_to_system_manual(self):
        """Send to tray on demand (from either progress dialog's Tray button,
        or the tray menu) — whichever batch is actually running (download or
        challan generation) is the one whose dialog gets hidden/counted."""
        if self._challan_running and self._challan_progress_dialog:
            n = len(self._challan_progress_dialog._targets)
        elif self._progress_dialog:
            n = len(self._progress_dialog._targets)
        else:
            n = 0
        self._tray.setToolTip(f"AayDocCapio — Running ({n} client(s)…)")
        self._tray_stop_act.setEnabled(True)
        self._tray_send_act.setVisible(False)  # hidden while already in tray
        self._tray.show()
        if self._progress_dialog:
            self._progress_dialog.hide()
        if self._challan_progress_dialog:
            self._challan_progress_dialog.hide()
        self.hide()
        self._tray_show_hint()

    def _tray_show_hint(self):
        """Show a Windows toast every time the app hides to tray."""
        notify_windows(
            "AayDocCapio is running in the background",
            "Click the tray icon to restore the window, or right-click for options.")

    def _tray_restore(self):
        """Restore main window and whichever progress dialog(s) from tray."""
        self._tray.hide()
        self._tray_send_act.setVisible(self.is_running or self._challan_running)
        self.show()
        self.showNormal()
        self.activateWindow()
        self.raise_()
        if self._progress_dialog:
            self._progress_dialog.show()
            self._progress_dialog.raise_()
            self._progress_dialog.activateWindow()
        if self._challan_progress_dialog:
            self._challan_progress_dialog.show()
            self._challan_progress_dialog.raise_()
            self._challan_progress_dialog.activateWindow()

    def _tray_stop_active(self):
        """Routes the tray menu's "Stop Batch" to whichever batch is
        actually running — download and challan generation each have their
        own stop function and own running flag."""
        if self.is_running:
            self.stop_automation()
        if self._challan_running:
            self.stop_challan_generation()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._tray_restore()

    def closeEvent(self, event):
        self.log("[System] AayDocCapio closed.")
        from automation.emailer import log_session_end
        log_session_end()
        if hasattr(self, "_tray"):
            self._tray.hide()
        super().closeEvent(event)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Menu bar ──────────────────────────────────────────────────────────
        menubar = self.menuBar()

        # Client Master menu
        cm_menu = menubar.addMenu("Client Master")
        def _micon(f):
            p = os.path.join(_bundled_dir(), "resources", "icons", f)
            return QIcon(QPixmap(p).scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)) if os.path.isfile(p) else QIcon()
        act_add      = QAction(_micon("menu_add_client.png"), "Add New Client",           self); act_add.triggered.connect(self._open_add_client)
        act_edit_cl  = QAction(_micon("icon_edit.png"),       "Edit Client\u2026",        self); act_edit_cl.triggered.connect(self._pick_and_edit_client)
        act_del_cl   = QAction(_micon("icon_delete.png"),     "Delete Client(s)\u2026",   self); act_del_cl.triggered.connect(self._pick_and_delete_clients)
        act_imp      = QAction(_micon("menu_import.png"),     "Import from CSV / Excel",  self); act_imp.triggered.connect(self.bulk_import)
        act_exp      = QAction(_micon("menu_export.png"),     "Export Client Data",       self); act_exp.triggered.connect(self.export_data)
        act_tpl      = QAction(_micon("menu_template.png"),   "Download Import Template", self); act_tpl.triggered.connect(self.generate_template)
        self._act_edit_cl = act_edit_cl
        self._act_del_cl  = act_del_cl
        cm_menu.addAction(act_add)
        cm_menu.addSeparator()
        cm_menu.addAction(act_edit_cl)
        cm_menu.addAction(act_del_cl)
        cm_menu.addSeparator()
        cm_menu.addAction(act_imp)
        cm_menu.addAction(act_exp)
        cm_menu.addSeparator()
        cm_menu.addAction(act_tpl)

        # Settings menu
        st_menu = menubar.addMenu("Settings")
        act_yr   = QAction(_micon("btn_scan.png"),          "Manage Assessment Years", self); act_yr.triggered.connect(self.open_manage_years)
        act_dir  = QAction(_micon("btn_browse_folder.png"), "Change Output Folder",    self); act_dir.triggered.connect(self.browse_output_dir)
        act_open = QAction(_micon("btn_browse.png"),        "Open Output Folder",      self); act_open.triggered.connect(self._open_output_folder)
        st_menu.addAction(act_yr)
        st_menu.addAction(act_dir)
        st_menu.addAction(act_open)
        st_menu.addSeparator()
        act_email = QAction(_micon("icon_email.png"), "Email Settings…", self); act_email.triggered.connect(self._open_email_settings)
        st_menu.addAction(act_email)
        st_menu.addSeparator()

        # Appearance submenu — built dynamically from THEMES registry
        appear_menu = st_menu.addMenu(_micon("menu_appearance.png"), "Appearance")
        _icons = {"light": "☀", "dark": "🌙"}
        for theme_key, theme_colors in THEMES.items():
            icon = _icons.get(theme_key, "●")
            act = QAction(f"{icon}  {theme_colors.name}", self)
            act.setCheckable(True)
            act.triggered.connect(lambda _, k=theme_key: self._apply_theme(k))
            setattr(self, f"_theme_{theme_key}_act", act)
            appear_menu.addAction(act)

        # Tools menu
        tools_menu = menubar.addMenu("Tools")
        act_convert = QAction(_micon("menu_export.png"),   "Convert 26AS TXT → Excel + HTML…", self)
        act_convert.triggered.connect(self._convert_26as_manual)
        tools_menu.addAction(act_convert)
        act_ais_convert = QAction(_micon("menu_template.png"), "Convert AIS JSON → Excel…", self)
        act_ais_convert.triggered.connect(self._convert_ais_json_manual)
        tools_menu.addAction(act_ais_convert)
        tools_menu.addSeparator()
        act_mail = QAction(_micon("btn_send.png"), "Mail Docs to Clients…", self)
        act_mail.triggered.connect(self._open_mail_docs)
        tools_menu.addAction(act_mail)
        self._tools_menu = tools_menu

        # E-Pay Tax menu (F-64) — home for challan-generation features;
        # future Type-of-Payment additions (Demand Payment, Block Assessment,
        # etc.) get new items here rather than new toolbar buttons each time.
        epay_menu = menubar.addMenu("E-Pay Tax")
        act_gen_challans = QAction("Generate Tax Challans…", self)
        act_gen_challans.triggered.connect(self._open_generate_challans_dialog)
        epay_menu.addAction(act_gen_challans)
        act_challan_template = QAction("Download Import Template…", self)
        act_challan_template.triggered.connect(self._download_challan_template)
        epay_menu.addAction(act_challan_template)
        self._epay_menu = epay_menu

        # Return Status menu (F-67) — a review/tracking screen opened
        # occasionally, not a per-run action like Downloads/E-Pay Tax, so it
        # only gets a menu entry, no toolbar button.
        return_status_menu = menubar.addMenu("Return Status")
        act_return_status = QAction("Check Processing Status…", self)
        act_return_status.triggered.connect(self._open_return_status_dialog)
        return_status_menu.addAction(act_return_status)
        self._return_status_menu = return_status_menu

        # Help menu
        help_menu = menubar.addMenu("Help")
        act_manual = QAction(_micon("menu_about.png"), "User Manual", self)
        act_manual.triggered.connect(self._open_user_manual)
        help_menu.addAction(act_manual)
        help_menu.addSeparator()
        smtp_help_action = QAction(_micon("btn_send_test.png"), "Email Setup Help…", self)
        smtp_help_action.triggered.connect(self._open_smtp_help)
        help_menu.addAction(smtp_help_action)
        help_menu.addSeparator()
        act_feedback = QAction(_micon("btn_view_log.png"), "Report Bug / Request Feature…", self)
        act_feedback.triggered.connect(self._open_feedback_picker)
        help_menu.addAction(act_feedback)
        help_menu.addSeparator()
        act_update = QAction(_micon("menu_about.png"), "Check for Updates…", self)
        act_update.triggered.connect(lambda: self._check_for_update(manual=True))
        help_menu.addAction(act_update)
        help_menu.addSeparator()
        about_action = QAction(_micon("menu_about.png"), "About AayDocCapio", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root = QVBoxLayout(root_widget)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        root.addWidget(self._mk_header())
        root.addWidget(self._mk_main_panel(), 1)
        root.addWidget(self._mk_footer())

    def _apply_theme(self, theme: str):
        """Switch theme by name and persist the choice."""
        self._current_theme = theme
        self.vault.update_setting("theme", theme)
        self.log(f"[System] Theme set to: {theme}")
        t = get_theme(theme)
        import ui._theme as _theme_mod
        _theme_mod._active_theme = t
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(t))
        if hasattr(self, "_theme_light_act"):
            for name, action in THEMES.items():
                act = getattr(self, f"_theme_{name}_act", None)
                if act:
                    act.setChecked(name == theme)
        self._repaint_theme(t)

    def _repaint_theme(self, t: ThemeColors = None):
        """Repaint widgets whose colours are set imperatively (not via QSS)."""
        if t is None:
            t = get_theme(self._current_theme)

        # ── Header ────────────────────────────────────────────────────────────
        if hasattr(self, "_hdr_frame"):
            self._hdr_frame.setStyleSheet(
                f"QFrame#header {{ background: {t.bg_window}; border: none; }}"
                f" QLabel {{ border: none; text-decoration: none; }}"
            )
        if hasattr(self, "_hdr_aay"):
            self._hdr_aay.setStyleSheet(
                f"color:{t.text_primary}; font-family:'Avenir Next'; font-size:36px;"
                f" font-weight:700; background:transparent; text-decoration:none; border:none;")
        if hasattr(self, "_hdr_capio"):
            self._hdr_capio.setStyleSheet(
                f"color:{t.accent}; font-family:'Avenir Next'; font-size:36px;"
                f" font-weight:700; background:transparent; text-decoration:none; border:none;")
        if hasattr(self, "_hdr_tm"):
            self._hdr_tm.setStyleSheet(
                f"color:{t.accent}; font-family:'Avenir Next'; font-size:14px;"
                f" font-weight:700; background:transparent; padding-bottom:18px;"
                f" text-decoration:none; border:none;")
        if hasattr(self, "_hdr_sep"):
            self._hdr_sep.setStyleSheet(
                f"color:{t.border}; font-size:22px; background:transparent; border:none;")
        if hasattr(self, "_hdr_tagline"):
            self._hdr_tagline.setStyleSheet(
                f"color:{t.text_muted}; font-family:'Arial'; font-size:13px;"
                f" font-weight:400; background:transparent; border:none;")
        for lbl in (getattr(self, "_hdr_version", None), getattr(self, "_hdr_copy", None)):
            if lbl:
                lbl.setStyleSheet(
                    f"color:{t.text_muted}; font-family:'Arial'; font-size:11px;"
                    f" background:transparent; border:none;")

        # ── Main panel + settings/control bar backgrounds ─────────────────────
        if hasattr(self, "_main_panel"):
            self._main_panel.setStyleSheet(f"background:{t.bg_window};")
        for bar in (getattr(self, "_settings_bar", None), getattr(self, "_control_bar", None)):
            if bar:
                bar.setStyleSheet(f"QFrame{{background:{t.bg_panel};border-radius:10px;}}")

        # ── Settings bar caption labels ───────────────────────────────────────
        for lbl in getattr(self, "_settings_caps", []):
            lbl.setStyleSheet(
                f"color:{t.text_muted};font-size:10px;font-weight:700;"
                f"letter-spacing:0.8px;background:transparent;")

        # ── Search box + status filter ────────────────────────────────────────
        if hasattr(self, "search_box"):
            self.search_box.setStyleSheet(
                f"QLineEdit{{background:{t.bg_input};border:1.5px solid {t.border};"
                f"border-radius:6px;padding:4px 10px;font-size:13px;"
                f"color:{t.text_primary};min-height:28px;}}"
                f"QLineEdit:focus{{border-color:{t.border_focus};background:{t.bg_input_focus};}}"
                f"QLineEdit::placeholder{{color:{t.text_muted};}}"
            )

        # ── dir_lbl + browse button ───────────────────────────────────────────
        if hasattr(self, "dir_lbl"):
            self.dir_lbl.setStyleSheet(
                f"color:{t.text_primary};font-size:12px;background:transparent;")
        _dir_btn_style = (
            f"QPushButton{{background:transparent;color:{t.text_primary};"
            f"border:1px solid {t.border};border-radius:6px;"
            f"padding:4px 12px;font-weight:bold;font-size:12px;}}"
            f"QPushButton:hover{{background:{t.bg_table_alt};}}"
            f"QPushButton:disabled{{color:{t.text_muted};border-color:{t.border};}}"
        )
        if hasattr(self, "_browse_btn"):
            self._browse_btn.setStyleSheet(_dir_btn_style)
        if hasattr(self, "_open_btn"):
            self._open_btn.setStyleSheet(_dir_btn_style)

        # ── chk_headless ──────────────────────────────────────────────────────
        if hasattr(self, "chk_headless"):
            self.chk_headless.setStyleSheet(
                f"QCheckBox{{font-size:12px;color:{t.text_muted};background:transparent;spacing:6px;}}"
                f"QCheckBox::indicator{{width:15px;height:15px;border:1.5px solid {t.border};"
                f"border-radius:3px;background:{t.bg_checkbox};}}"
                f"QCheckBox::indicator:checked{{background:{t.accent};border-color:{t.accent};}}")

        # ── Run button ────────────────────────────────────────────────────────
        if hasattr(self, "btn_run"):
            self.btn_run.setStyleSheet(
                f"QToolButton{{background:{t.accent};color:{t.accent_text};border:none;"
                f"border-radius:6px;font-size:13px;font-weight:600;padding:0 14px;}}"
                f"QToolButton:hover{{background:{t.accent_hover};}}"
                f"QToolButton::menu-button{{border:none;width:20px;}}"
                f"QToolButton:disabled{{background:{t.border};color:{t.text_muted};}}")
            if hasattr(self.btn_run, "menu") and self.btn_run.menu():
                self.btn_run.menu().setStyleSheet(
                    f"QMenu{{background:{t.bg_menu};border:1.5px solid {t.border_menu};"
                    f"border-radius:8px;padding:4px 0;}}"
                    f"QMenu::item{{padding:8px 18px;font-size:13px;color:{t.text_primary};}}"
                    f"QMenu::item:selected{{background:{t.accent};color:{t.accent_text};}}"
                    f"QMenu::separator{{height:1px;background:{t.border_menu};margin:4px 0;}}")

        # ── CLIENTS caption + selected count label ────────────────────────────
        if hasattr(self, "_lbl_clients_cap"):
            self._lbl_clients_cap.setStyleSheet(
                f"color:{t.text_muted};font-size:10px;font-weight:700;"
                f"letter-spacing:0.8px;background:transparent;")
        if hasattr(self, "lbl_selected"):
            self.lbl_selected.setStyleSheet(
                f"color:{t.accent};font-size:11px;font-weight:bold;background:transparent;")

        # ── Header checkbox (select-all) ──────────────────────────────────────
        if hasattr(self, "header_cb"):
            _chk_path2 = self.checkmark_path.replace("\\", "/")
            self.header_cb.setStyleSheet(
                f"QCheckBox{{background:transparent;}}"
                f"QCheckBox::indicator{{width:15px;height:15px;border:1.5px solid {t.border};"
                f"border-radius:3px;background:{t.bg_checkbox};}}"
                f"QCheckBox::indicator:hover{{border-color:{t.border_focus};}}"
                f"QCheckBox::indicator:checked{{background:{t.accent};border-color:{t.accent};"
                f"image:url('{_chk_path2}');}}"
            )

        # ── Table widget stylesheet + header ──────────────────────────────────
        if hasattr(self, "client_table"):
            chk_path = self.checkmark_path.replace("\\", "/")
            chk_ss = (
                "QCheckBox { background: transparent; }"
                f"QCheckBox::indicator {{ width: 15px; height: 15px; border: 1.5px solid {t.border};"
                f" border-radius: 3px; background: {t.bg_checkbox}; }}"
                f"QCheckBox::indicator:hover {{ border-color: {t.border_focus}; }}"
                f"QCheckBox::indicator:checked {{ background-color: {t.accent}; border-color: {t.accent};"
                f" image: url('{chk_path}'); }}"
            )
            self.client_table.setStyleSheet(
                f"QTableWidget {{ border: 1.5px solid {t.border}; border-radius: 8px;"
                f" background: {t.bg_table}; outline: 0; gridline-color: {t.grid}; }}"
                f"QTableWidget::item {{ border-bottom: 1px solid {t.grid}; padding: 5px; color: {t.text_primary}; }}"
                + chk_ss
            )
            self.client_table.horizontalHeader().setStyleSheet(
                f"QHeaderView::section {{ background-color: {t.bg_header}; border: none;"
                f" border-right: 1px solid {t.border}; border-bottom: 1px solid {t.border};"
                f" font-weight: bold; color: {t.text_muted}; font-size: 11px; height: 34px; }}"
            )
            self.refresh_grid()

        # ── Log box ───────────────────────────────────────────────────────────
        if hasattr(self, "log_box"):
            self.log_box.setStyleSheet(
                f"QTextEdit {{ background: {t.bg_log}; border: none;"
                f" font-family: '{_MONO_FONT}', monospace;"
                f" font-size: 11px; color: {t.text_log}; padding: 8px 16px; }}"
            )

    def _open_user_manual(self):
        import webbrowser
        from ui.user_manual import _write_user_manual_html
        path = _write_user_manual_html()
        webbrowser.open("file:///" + path.replace(os.sep, "/"))

    def _open_smtp_help(self):
        import webbrowser
        from ui.smtp_help import _write_smtp_help_html
        path = _write_smtp_help_html()
        webbrowser.open("file:///" + path.replace(os.sep, "/"))

    def _open_feedback_picker(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Report Bug / Request Feature")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("What would you like to send?")
        msg.setInformativeText(
            "AayDocCapio will open GitHub in your browser with a structured form. "
            "Review the details before submitting, and do not paste passwords or client PANs."
        )
        bug_btn = msg.addButton("Report Bug", QMessageBox.ButtonRole.AcceptRole)
        feature_btn = msg.addButton("Request Feature", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == bug_btn:
            self._open_github_issue("bug_report.yml", "[Bug] ")
        elif clicked == feature_btn:
            self._open_github_issue("feature_request.yml", "[Feature] ")

    def _open_github_issue(self, template_name: str, title_prefix: str):
        query = urlencode({
            "template": template_name,
            "title": title_prefix,
        })
        QDesktopServices.openUrl(QUrl(f"https://github.com/dkbholusaria/AayDocCapio/issues/new?{query}"))

    def _show_about(self):
        import webbrowser
        dlg = QDialog(self)
        dlg.setWindowTitle("About AayDocCapio")
        dlg.setFixedSize(500, 520)
        _ab = _t()
        dlg.setStyleSheet(
            f"QDialog {{ background:{_ab.bg_window}; }}"
            f"QLabel {{ border:none; background:transparent; color:{_ab.text_primary}; }}"
            f"QLabel[link=true] {{ color:{_ab.accent}; }}"
        )

        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(36, 28, 36, 28)
        vl.setSpacing(0)

        # ── App logo + name ───────────────────────────────────────────────────
        logo_row = QHBoxLayout(); logo_row.setSpacing(14)
        icon_lbl = QLabel()
        icon_path = os.path.join(_bundled_dir(), "resources", "app_icon.png")
        if os.path.exists(icon_path):
            icon_lbl.setPixmap(QPixmap(icon_path).scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                                          Qt.TransformationMode.SmoothTransformation))
        logo_row.addWidget(icon_lbl)
        name_col = QVBoxLayout(); name_col.setSpacing(2)
        name_lbl = QLabel(
            f'<span style="color:{_ab.text_primary};font-family:\'Avenir Next\';font-size:22px;font-weight:700;">AayDoc </span>'
            f'<span style="color:{_ab.accent};font-family:\'Avenir Next\';font-size:22px;font-weight:700;">Capio™</span>'
        )
        ver_lbl = QLabel(f"Version {APP_VERSION}")
        ver_lbl.setStyleSheet(f"color:{_ab.text_muted}; font-size:12px;")
        name_col.addWidget(name_lbl); name_col.addWidget(ver_lbl)
        logo_row.addLayout(name_col); logo_row.addStretch()
        vl.addLayout(logo_row)
        vl.addSpacing(14)

        desc = QLabel("Automates the secure bulk retrieval of Form 26AS, AIS and TIS directly from the Income Tax e-Filing Portal.")
        desc.setStyleSheet(f"color:{_ab.text_primary}; font-size:13px;")
        desc.setWordWrap(True)
        vl.addWidget(desc)
        vl.addSpacing(12)

        # Name explanation
        name_box = QFrame()
        name_box.setStyleSheet(f"QFrame {{ background:{_ab.bg_panel}; border-radius:6px; border:1px solid {_ab.accent}; }}")
        nb_l = QVBoxLayout(name_box)
        nb_l.setContentsMargins(14, 10, 14, 10)
        nb_l.setSpacing(6)
        name_head = QLabel("Why AayDoc Capio?")
        name_head.setStyleSheet(f"color:{_ab.accent}; font-size:11px; font-weight:700; letter-spacing:0.5px; background:transparent; border:none;")
        name_exp = QLabel(
            f'<b style="color:{_ab.text_primary};">Aay</b> (Income) · '
            f'<b style="color:{_ab.text_primary};">Doc</b> (Documents) · '
            f'<b style="color:{_ab.accent};">Capio</b> <span style="color:{_ab.text_muted};">(Latin: To Obtain)</span>'
        )
        name_exp.setStyleSheet("font-size:13px; background:transparent; border:none;")
        name_sub = QLabel("AayDoc Capio is designed to securely retrieve and deliver income tax documents, "
                          "eliminating repetitive manual downloads and improving efficiency for tax professionals.")
        name_sub.setStyleSheet(f"color:{_ab.text_primary}; font-size:12px; background:transparent; border:none;")
        name_sub.setWordWrap(True)
        nb_l.addWidget(name_head)
        nb_l.addWidget(name_exp)
        nb_l.addWidget(name_sub)
        vl.addWidget(name_box)
        vl.addSpacing(16)

        # ── Divider ───────────────────────────────────────────────────────────
        div1 = QFrame(); div1.setFrameShape(QFrame.Shape.HLine)
        div1.setStyleSheet(f"background:{_ab.border}; border:none; max-height:1px;")
        vl.addWidget(div1)
        vl.addSpacing(16)

        # ── Developer info ────────────────────────────────────────────────────
        dev_title = QLabel("Contact Us")
        dev_title.setStyleSheet(f"color:{_ab.text_muted}; font-size:10px; font-weight:700; letter-spacing:1px;")
        vl.addWidget(dev_title)
        vl.addSpacing(8)

        def _link_row(icon_file, display_text, url=None):
            row = QHBoxLayout(); row.setSpacing(10); row.setContentsMargins(0,0,0,0)
            icon_l = QLabel()
            icon_l.setFixedSize(22, 22)
            icon_path = os.path.join(_bundled_dir(), "resources", "icons", icon_file)
            if os.path.exists(icon_path):
                icon_l.setPixmap(QPixmap(icon_path).scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio,
                                                            Qt.TransformationMode.SmoothTransformation))
            icon_l.setStyleSheet("background:transparent; border:none;")
            row.addWidget(icon_l)
            if url:
                lbl = QLabel(f'<a href="{url}" style="color:{_ab.accent}; text-decoration:none;">{display_text}</a>')
                lbl.setOpenExternalLinks(True)
                lbl.setStyleSheet("background:transparent; border:none; font-size:13px;")
            else:
                lbl = QLabel(display_text)
                lbl.setStyleSheet(f"color:{_ab.text_primary}; font-size:13px; font-weight:600; background:transparent; border:none;")
            row.addWidget(lbl)
            row.addStretch()
            return row

        vl.addLayout(_link_row("icon_person.png",   "CA. Deepak Bhholusaria"))
        vl.addSpacing(6)
        vl.addLayout(_link_row("icon_email.png",    "deepak@ailearrning.guru",          "mailto:deepak@ailearrning.guru"))
        vl.addSpacing(6)
        vl.addLayout(_link_row("icon_linkedin.png", "linkedin.com/in/bhholusaria",      "https://www.linkedin.com/in/bhholusaria/"))
        vl.addSpacing(6)
        vl.addLayout(_link_row("icon_vcard.png",    "www.ailearrning.guru",             "https://www.ailearrning.guru"))
        vl.addSpacing(6)
        vl.addLayout(_link_row("icon_virtualcard.png", "E-Visiting Card",               "https://deepak.bholusaria.com"))
        vl.addSpacing(16)

        # ── Divider ───────────────────────────────────────────────────────────
        div2 = QFrame(); div2.setFrameShape(QFrame.Shape.HLine)
        div2.setStyleSheet(f"background:{_ab.border}; border:none; max-height:1px;")
        vl.addWidget(div2)
        vl.addSpacing(12)

        copy = QLabel("© 2026 Deepak Bhholusaria. All rights reserved.")
        copy.setStyleSheet(f"color:{_ab.text_muted}; font-size:11px;")
        vl.addWidget(copy)
        vl.addStretch()

        # ── Close button ──────────────────────────────────────────────────────
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(100)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{_ab.accent}; color:{_ab.accent_text}; border:none; border-radius:6px; padding:8px 16px; font-size:13px; }}"
            f"QPushButton:hover {{ background:{_ab.accent_hover}; }}")
        close_btn.clicked.connect(dlg.accept)
        btn_row = QHBoxLayout(); btn_row.addStretch(); btn_row.addWidget(close_btn)
        vl.addLayout(btn_row)

        dlg.exec()

    def _mk_header(self):
        hdr = QFrame()
        hdr.setFixedHeight(110)
        hdr.setObjectName("header")
        self._hdr_frame = hdr
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(32, 0, 32, 0)
        hl.setSpacing(0)

        # App icon — large enough to anchor the header
        icon_label = QLabel()
        icon_path = os.path.join(_bundled_dir(), "resources", "app_icon.png")
        if os.path.exists(icon_path):
            icon_label.setPixmap(
                QPixmap(icon_path).scaled(106, 106, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
            )
        hl.addWidget(icon_label)
        hl.addSpacing(18)

        # Name + tagline stacked
        name_block = QWidget()
        name_block.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(name_block)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(3)

        title_row = QWidget()
        title_row.setStyleSheet("background:transparent;")
        tl = QHBoxLayout(title_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        aay = QLabel("AayDoc ")
        self._hdr_aay = aay
        tl.addWidget(aay)

        capio = QLabel("Capio")
        self._hdr_capio = capio
        tl.addWidget(capio)

        tm = QLabel("™")
        self._hdr_tm = tm
        tl.addWidget(tm)

        # Separator + tagline inline with title
        sep = QLabel("  |  ")
        self._hdr_sep = sep
        sep.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tl.addWidget(sep)

        tagline = QLabel("Tax Documents. Delivered to You.")
        self._hdr_tagline = tagline
        tagline.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tl.addWidget(tagline)
        tl.addStretch()

        title = title_row

        vl.addStretch()
        vl.addWidget(title)
        vl.addStretch()

        hl.addWidget(name_block)
        hl.addStretch()

        # Copyright + version on the right
        meta_block = QWidget()
        meta_block.setStyleSheet("background:transparent;")
        ml = QVBoxLayout(meta_block)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(2)
        ml.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        version_lbl = QLabel(f"v{APP_VERSION}")
        self._hdr_version = version_lbl
        version_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)

        copy_lbl = QLabel("© 2026 Deepak Bhholusaria")
        self._hdr_copy = copy_lbl
        copy_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)

        ml.setSpacing(1)
        ml.addStretch()
        ml.addWidget(version_lbl)
        ml.addWidget(copy_lbl)

        self._hdr_update_lnk = QLabel()
        self._hdr_update_lnk.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._hdr_update_lnk.setOpenExternalLinks(False)
        self._hdr_update_lnk.linkActivated.connect(self._on_update_link_clicked)
        self._hdr_update_lnk.setFixedHeight(16)
        ml.addWidget(self._hdr_update_lnk)

        ml.addStretch()

        hl.addWidget(meta_block)
        hl.addSpacing(12)

        # ⓘ About button
        about_btn = QPushButton("ⓘ")
        about_btn.setFixedSize(32, 32)
        about_btn.setToolTip("About AayDocCapio")
        about_btn.setStyleSheet(
            "QPushButton { background:transparent; border:none; font-size:20px; color:#DC2626; }"
            "QPushButton:hover { color:#B91C1C; }")
        about_btn.clicked.connect(self._show_about)
        hl.addWidget(about_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        return hdr

    def _mk_main_panel(self):
        panel = QWidget()
        self._main_panel = panel
        panel.setStyleSheet(f"background:{_t().bg_window};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 8)
        layout.setSpacing(6)

        settings = self._mk_settings_bar()
        settings.setGraphicsEffect(_shadow(18, 3, 18))
        layout.addWidget(settings)

        # Grid label row
        grid_hdr_row = QHBoxLayout()
        self._lbl_clients_cap = _lbl("CLIENTS", 10, bold=True, color=_t().text_muted)
        grid_hdr_row.addWidget(self._lbl_clients_cap)
        grid_hdr_row.addStretch()
        self.lbl_selected = _lbl("0 selected", 11, bold=True, color=_t().accent)
        grid_hdr_row.addWidget(self.lbl_selected)
        layout.addLayout(grid_hdr_row)

        # Search / filter bar
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.setContentsMargins(0, 0, 0, 0)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍  Search by name, PAN or status...")
        self.search_box.setFixedHeight(28)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.search_box.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.search_box, 1)

        self.status_filter = StyledComboBox()
        self.status_filter.addItems([
            "All statuses",
            "✅  Downloaded",
            "⚠  Partially Completed",
            "❌  Failed",
            "🕐  Queued / Pending",
            "—  Not run yet",
        ])
        self.status_filter.setFixedHeight(28)
        self.status_filter.setFixedWidth(185)
        self.status_filter.currentIndexChanged.connect(lambda _: self._apply_filter(self.search_box.text()))
        filter_row.addWidget(self.status_filter)

        layout.addLayout(filter_row)

        layout.addWidget(self._mk_client_table(), 1)

        ctrl = self._mk_control_bar()
        ctrl.setGraphicsEffect(_shadow(18, 3, 18))
        layout.addWidget(ctrl)
        return panel

    def _mk_settings_bar(self):
        bar = QFrame()
        self._settings_bar = bar
        bar.setFixedHeight(68)
        bar.setStyleSheet(f"QFrame{{background:{_t().bg_panel};border-radius:10px;}}")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(20, 0, 20, 0)
        hl.setSpacing(0)
        hl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._settings_caps = []

        def _cap(text):
            l = QLabel(text)
            self._settings_caps.append(l)
            return l

        def _divider():
            d = QFrame(); d.setFrameShape(QFrame.Shape.VLine)
            d.setFixedSize(1, 32)
            d.setStyleSheet(f"background:{_t().border};border:none;")
            return d

        # ── Assessment Year (F-14: multi-select, one batch can cover several
        # years) ────────────────────────────────────────────────────────────
        # Deliberately NOT restored from a saved setting — starts unchecked
        # on every launch so a stale multi-year selection from a previous
        # session can't silently apply to a new run.
        self._ay_entries = self._load_ay_list()
        ay_labels = [e["label"] for e in self._ay_entries if e.get("enabled", True)]
        self.ay_combo = CheckableComboBox(placeholder="Select AY/TY")
        for _label in ay_labels:
            self.ay_combo.add_item(_label, checked=False)
        self.ay_combo.setFixedWidth(220)
        self.ay_combo.model_.itemChanged.connect(lambda _item: self._log_ay_selection())
        self.ay_combo.model_.itemChanged.connect(lambda _item: self.refresh_grid())

        manage_btn = QPushButton("⚙")
        manage_btn.setFixedSize(24, 24)
        manage_btn.setToolTip("Manage Years")
        manage_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;font-size:14px;color:{_t().text_muted};}}"
            f"QPushButton:hover{{color:{_t().accent};}}")
        manage_btn.clicked.connect(self.open_manage_years)

        ay_col = QWidget(); ay_col.setStyleSheet("background:transparent;")
        ay_vl = QVBoxLayout(ay_col); ay_vl.setContentsMargins(0,0,0,0); ay_vl.setSpacing(2)
        ay_vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        ay_vl.addWidget(_cap("ASSESSMENT YEAR"))
        ay_hl = QHBoxLayout(); ay_hl.setContentsMargins(0,0,0,0); ay_hl.setSpacing(5)
        ay_hl.addWidget(self.ay_combo); ay_hl.addWidget(manage_btn)
        ay_vl.addLayout(ay_hl)
        hl.addWidget(ay_col)

        hl.addSpacing(28); hl.addWidget(_divider()); hl.addSpacing(28)

        # ── Output Directory ──────────────────────────────────────────────────
        # Prefer Windows path whenever USERPROFILE is set (native Windows or WSL).
        # Use saved path if it exists and is reachable — regardless of format
        # (Windows drive, UNC //wsl.localhost/..., SUBST, junction, etc.).
        # Only fall back to the Windows default when running on Windows and the
        # saved path is a bare Linux path (/home/...) that Windows cannot reach.
        _saved_dir = self.vault.get_setting("download_root_dir", "")
        _on_windows = sys.platform == "win32"
        _is_bare_linux = (
            _saved_dir.startswith("/")
            and not _saved_dir.startswith("//")
            and not _saved_dir.startswith("\\\\")
        )
        if _saved_dir and not (_on_windows and _is_bare_linux) and os.path.isdir(_saved_dir):
            default_dir = _saved_dir
        else:
            default_dir = _default_download_dir()
            self.vault.update_setting("download_root_dir", default_dir)
        if sys.platform == "win32":
            default_dir = default_dir.replace("/", "\\")
        self.dir_lbl = QLabel(default_dir)
        self.dir_lbl.setStyleSheet(f"color:{_t().text_primary};font-size:12px;background:transparent;")
        self.dir_lbl.setWordWrap(False)
        self.dir_lbl.setMaximumWidth(320)
        self.dir_lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        browse_btn = _btn("Browse", "outline", height=26, icon="btn_browse.png")
        self._browse_btn = browse_btn
        browse_btn.clicked.connect(self.browse_output_dir)

        open_btn = _btn("Open", "outline", height=26, icon="btn_open.png")
        open_btn.clicked.connect(self._open_output_folder)
        self._open_btn = open_btn

        dir_row = QHBoxLayout(); dir_row.setContentsMargins(0,0,0,0); dir_row.setSpacing(8)
        dir_row.addWidget(self.dir_lbl); dir_row.addWidget(browse_btn); dir_row.addWidget(open_btn)

        dir_col = QWidget(); dir_col.setStyleSheet("background:transparent;")
        dir_col.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        dir_vl = QVBoxLayout(dir_col); dir_vl.setContentsMargins(0,0,0,0); dir_vl.setSpacing(2)
        dir_vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        dir_vl.addWidget(_cap("OUTPUT DIRECTORY"))
        dir_vl.addLayout(dir_row)
        hl.addWidget(dir_col)
        hl.addStretch()

        return bar

    # Column indices for client table
    _TC_CHK    = 0
    _TC_NAME   = 1
    _TC_PAN    = 2
    _TC_DOB    = 3
    _TC_STATUS = 4
    _TC_TS     = 5   # Last Download Time
    _TC_PATH   = 6
    _TC_ACTS   = 7

    def _mk_client_table(self):
        self.client_table = QTableWidget(0, 8)
        self.client_table.setHorizontalHeaderLabels([
            "", "Name  ⇅", "PAN  ⇅", "Date of Birth",
            "Last Download Status", "Last Download Time", "Last Saved Location", ""
        ])

        _tbl = _t()
        self.client_table.horizontalHeader().setStyleSheet(
            f"QHeaderView::section {{ background-color: {_tbl.bg_header}; border: none; "
            f"border-right: 1px solid {_tbl.border}; border-bottom: 1px solid {_tbl.border}; "
            f"font-weight: bold; color: {_tbl.text_muted}; font-size: 11px; height: 34px; }}"
        )
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.client_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.client_table.setShowGrid(True)
        self.client_table.setAlternatingRowColors(False)

        chk_path = self.checkmark_path.replace("\\", "/")
        checkbox_style = (
            "QCheckBox { background: transparent; }"
            f"QCheckBox::indicator {{ width: 15px; height: 15px; border: 1.5px solid {_tbl.border}; border-radius: 3px; background: {_tbl.bg_checkbox}; }}"
            f"QCheckBox::indicator:hover {{ border-color: {_tbl.border_focus}; }}"
            f"QCheckBox::indicator:checked {{ background-color: {_tbl.accent}; border-color: {_tbl.accent}; image: url('{chk_path}'); }}"
        )
        self.client_table.setStyleSheet(
            f"QTableWidget {{ border: 1.5px solid {_tbl.border}; border-radius: 8px; background: {_tbl.bg_table}; outline: 0; gridline-color: {_tbl.grid}; }}"
            f"QTableWidget::item {{ border-bottom: 1px solid {_tbl.grid}; padding: 5px; }}"
            + checkbox_style
        )

        for col, align in [
            (self._TC_NAME,   Qt.AlignmentFlag.AlignCenter),
            (self._TC_PAN,    Qt.AlignmentFlag.AlignCenter),
            (self._TC_DOB,    Qt.AlignmentFlag.AlignCenter),
            (self._TC_STATUS, Qt.AlignmentFlag.AlignCenter),
            (self._TC_TS,     Qt.AlignmentFlag.AlignCenter),
            (self._TC_PATH,   Qt.AlignmentFlag.AlignCenter),
            (self._TC_ACTS,   Qt.AlignmentFlag.AlignCenter),
        ]:
            item = self.client_table.horizontalHeaderItem(col)
            if item:
                item.setTextAlignment(align)

        self.client_table.setColumnWidth(self._TC_CHK,    45)
        self.client_table.setColumnWidth(self._TC_PAN,   130)
        self.client_table.setColumnWidth(self._TC_STATUS, 170)
        self.client_table.setColumnWidth(self._TC_TS,    155)
        self.client_table.setColumnWidth(self._TC_ACTS,   52)
        self.client_table.setColumnHidden(self._TC_DOB, True)   # F-39: DOB is PII, hidden

        header = self.client_table.horizontalHeader()
        header.setSectionResizeMode(self._TC_CHK,    QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self._TC_NAME,   QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._TC_PAN,    QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self._TC_DOB,    QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self._TC_STATUS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self._TC_TS,     QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self._TC_PATH,   QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self._TC_ACTS,   QHeaderView.ResizeMode.Interactive)

        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._on_header_clicked)
        self._current_sort_col = -1
        self._current_sort_order = Qt.SortOrder.AscendingOrder

        self.client_table.cellClicked.connect(self._on_cell_clicked)
        self.client_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.client_table.setMouseTracking(True)
        self.client_table.viewport().setMouseTracking(True)
        self.client_table.cellEntered.connect(self._on_cell_entered)
        self._hovered_acts_row = -1

        self.header_cb = QCheckBox(header)
        self.header_cb.setFixedSize(18, 18)
        self.header_cb.setStyleSheet(checkbox_style)
        self.header_cb.toggled.connect(self.toggle_select_all)
        header.geometriesChanged.connect(self._position_header_checkbox)

        self.client_table.setMinimumHeight(200)
        return self.client_table

    def _position_header_checkbox(self):
        if not hasattr(self, "client_table") or not hasattr(self, "header_cb"):
            return
        header = self.client_table.horizontalHeader()
        x = header.sectionPosition(0)
        w = header.sectionSize(0)
        h = header.height()
        cb_x = x + (w - 18) // 2
        cb_y = (h - 18) // 2
        self.header_cb.move(cb_x, cb_y)

    def _on_cell_double_clicked(self, row, col):
        if col == self._TC_STATUS:
            item = self.client_table.item(row, self._TC_STATUS)
            if not item:
                return
            full_text = item.toolTip() or item.text()
            if not full_text:
                return
            name_item = self.client_table.item(row, self._TC_NAME)
            name = name_item.text() if name_item else ""
            mb = QMessageBox(self)
            mb.setWindowTitle("Last Download Status")
            mb.setText(f"<b>{name}</b>" if name else "Status Detail")
            mb.setInformativeText(full_text)
            mb.setStandardButtons(QMessageBox.StandardButton.Ok)
            # Force a minimum width via a hidden spacer in the grid layout
            from PyQt6.QtWidgets import QSpacerItem, QSizePolicy
            spacer = QSpacerItem(420, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
            layout = mb.layout()
            layout.addItem(spacer, layout.rowCount(), 0, 1, layout.columnCount())
            mb.exec()

    def _on_cell_clicked(self, row, col):
        if hasattr(self, "ay_combo") and self.ay_combo._popup_was_open:
            return

        if col == self._TC_ACTS:
            item = self.client_table.item(row, self._TC_ACTS)
            if not item:
                return
            a = item.data(Qt.ItemDataRole.UserRole + 1)
            if not a:
                return
            a_id = a.get("id")
            _mt = _t()
            menu = QMenu(self)
            menu.setStyleSheet(
                f"QMenu {{ background:{_mt.bg_menu}; border:1.5px solid {_mt.border_menu}; border-radius:6px; padding:4px 0; }}"
                f"QMenu::item {{ padding:8px 20px; font-size:12px; color:{_mt.text_primary}; }}"
                f"QMenu::item:selected {{ background:{_mt.accent}; color:{_mt.accent_text}; }}"
                f"QMenu::separator {{ height:1px; background:{_mt.border_menu}; margin:3px 0; }}"
            )
            def _cicon(f):
                p = os.path.join(_bundled_dir(), "resources", "icons", f)
                return QIcon(QPixmap(p).scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)) if os.path.isfile(p) else QIcon()
            menu.addAction(_cicon("icon_edit.png"),   "Edit Client",   lambda av=a:     self._open_edit_client(av))
            menu.addSeparator()
            menu.addAction(_cicon("icon_delete.png"), "Delete Client", lambda id_=a_id: self.delete_assessee(id_))
            menu.addSeparator()
            act_log = menu.addAction(_cicon("btn_scan.png"), "View Log")
            act_log.triggered.connect(lambda checked=False, _a=a: self._show_log_history(_a))
            # Right-align menu: top-right of menu anchored to bottom-right of … cell
            from PyQt6.QtCore import QPoint
            rect    = self.client_table.visualItemRect(item)
            cell_br = self.client_table.viewport().mapToGlobal(rect.bottomRight())
            menu_w  = menu.sizeHint().width()
            menu.exec(QPoint(cell_br.x() - menu_w, cell_br.y()))
            return

        if col in (self._TC_CHK, self._TC_PATH):
            return
        # Toggle checkbox for any other column click
        cb_container = self.client_table.cellWidget(row, self._TC_CHK)
        if cb_container:
            cb = cb_container.findChild(QCheckBox)
            if cb:
                cb.setChecked(not cb.isChecked())

    def _on_cell_entered(self, row, col):
        pass

    def _on_header_clicked(self, logical_index):
        if logical_index not in (self._TC_NAME, self._TC_PAN):
            return
            
        header = self.client_table.horizontalHeader()
        
        # Toggle or initialize sort
        if self._current_sort_col == logical_index:
            self._current_sort_order = (
                Qt.SortOrder.DescendingOrder 
                if self._current_sort_order == Qt.SortOrder.AscendingOrder 
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._current_sort_col = logical_index
            self._current_sort_order = Qt.SortOrder.AscendingOrder
            
        # Visually show active sort indicator
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self._current_sort_col, self._current_sort_order)
        
        # Perform sort
        self.client_table.setSortingEnabled(True)
        self.client_table.sortByColumn(self._current_sort_col, self._current_sort_order)
        self.client_table.setSortingEnabled(False)
        
        for row_idx in range(self.client_table.rowCount()):
            item = self.client_table.item(row_idx, self._TC_NAME)
            if item:
                row_id = item.data(Qt.ItemDataRole.UserRole)
                row_selected = row_id in self.selected_ids
                self._apply_row_style(row_idx, row_selected, row_idx)

    def _mk_control_bar(self):
        bar = QFrame()
        self._control_bar = bar
        bar.setFixedHeight(54)
        bar.setStyleSheet(f"QFrame{{background:{_t().bg_panel};border-radius:10px;}}")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(22, 0, 16, 0)

        # Headless toggle — when checked, the automation browser runs hidden.
        # Default ON; uncheck to watch progress or handle a CAPTCHA.
        self.chk_headless = QCheckBox("Run in background (hide browser)")
        self.chk_headless.setChecked(True)
        self.chk_headless.setToolTip(
            "When ON, the automation Chrome window is hidden (headless).\n"
            "Keep OFF to watch progress or handle any CAPTCHA.")
        _ck = _t()
        self.chk_headless.setStyleSheet(
            f"QCheckBox{{font-size:12px;color:{_ck.text_muted};background:transparent;spacing:6px;}}"
            f"QCheckBox::indicator{{width:15px;height:15px;border:1.5px solid {_ck.border};"
            f"border-radius:3px;background:{_ck.bg_checkbox};}}"
            f"QCheckBox::indicator:checked{{background:{_ck.accent};border-color:{_ck.accent};}}")
        hl.addWidget(self.chk_headless)

        _auto_min_saved = self.vault.get_setting("auto_minimise", False) if hasattr(self, "vault") else False
        self.chk_auto_minimise = QCheckBox("Send to system tray when download starts")
        self.chk_auto_minimise.setChecked(bool(_auto_min_saved))
        self.chk_auto_minimise.setToolTip(
            "Hide the app to the system tray when a batch download begins.\n"
            "Click the tray icon or right-click → Restore to bring it back.\n"
            "The app restores automatically when the batch finishes.")
        self.chk_auto_minimise.setStyleSheet(self.chk_headless.styleSheet())
        self.chk_auto_minimise.stateChanged.connect(
            lambda v: self.vault.update_setting("auto_minimise", bool(v)))
        hl.addWidget(self.chk_auto_minimise)

        hl.addStretch()



        # ── Email Docs button ─────────────────────────────────────────────────
        self.btn_email_docs = _btn("Email Docs", "secondary", height=34, icon="btn_send.png")
        self.btn_email_docs.setToolTip("Mail downloaded tax documents to clients")
        self.btn_email_docs.clicked.connect(self._open_mail_docs)
        hl.addWidget(self.btn_email_docs)
        hl.addSpacing(8)

        # ── Download dropdown (split-style: label + arrow) ────────────────────
        # F-56 Phase 3: a single "Download" button opens a checkbox picker
        # (DownloadPickerDialog) instead of a dropdown menu with one action
        # per document type — lets a user select any combination for one run.
        self.btn_run = QToolButton()
        self.btn_run.setText("  Downloads")
        self.btn_run.setFixedHeight(34)
        self.btn_run.setMinimumWidth(130)
        from ui.helpers import _icon_path
        from PyQt6.QtGui import QIcon, QPixmap
        from PyQt6.QtCore import QSize, Qt
        _run_icon_p = _icon_path("btn_run.png")
        if _run_icon_p:
            _run_px = QPixmap(_run_icon_p).scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.btn_run.setIcon(QIcon(_run_px))
            self.btn_run.setIconSize(QSize(20, 20))
            self.btn_run.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_run.setStyleSheet(
            "QToolButton{"
            "  background:#16A34A; color:#FFFFFF; border:none;"
            "  border-radius:8px; font-size:13px; font-weight:600; padding:0 14px;"
            "}"
            "QToolButton:hover{ background:#15803D; }"
            "QToolButton:disabled{ background:#D1FAE5; color:#6EE7B7; }"
        )
        self.btn_run.clicked.connect(self._open_download_picker)
        hl.addWidget(self.btn_run)
        hl.addSpacing(8)

        # F-64: separate button for the "active" (record-creating) e-Pay Tax
        # flow — styled distinctly from Downloads' green since this one
        # generates real portal records rather than just fetching files.
        self.btn_epay = QToolButton()
        self.btn_epay.setText("  🧾 E-Pay Tax")
        self.btn_epay.setFixedHeight(34)
        self.btn_epay.setMinimumWidth(130)
        self.btn_epay.setStyleSheet(
            "QToolButton{"
            "  background:#2563EB; color:#FFFFFF; border:none;"
            "  border-radius:8px; font-size:13px; font-weight:600; padding:0 14px;"
            "}"
            "QToolButton:hover{ background:#1D4ED8; }"
            "QToolButton:disabled{ background:#BFDBFE; color:#EFF6FF; }"
        )
        self.btn_epay.clicked.connect(self._open_generate_challans_dialog)
        hl.addWidget(self.btn_epay)
        hl.addSpacing(8)

        # ── Exit button ───────────────────────────────────────────────────────
        self.btn_exit = _btn("Exit", "danger", height=34, icon="btn_cancel.png")
        self.btn_exit.setToolTip("Close AayDocCapio")
        self.btn_exit.clicked.connect(self.close)
        hl.addWidget(self.btn_exit)

        # ── AIS status line (hidden until Request AIS runs) ───────────────────
        self.ais_status_bar = QFrame()
        self.ais_status_bar.setFixedHeight(28)
        self.ais_status_bar.setStyleSheet(
            "QFrame{background:#FFF7ED;border-top:1px solid #FED7AA;}")
        self.ais_status_bar.setVisible(False)
        asl = QHBoxLayout(self.ais_status_bar)
        asl.setContentsMargins(22, 0, 16, 0)
        self.ais_status_lbl = QLabel()
        self.ais_status_lbl.setStyleSheet(
            "color:#92400E; font-size:11px; background:transparent;")
        asl.addWidget(self.ais_status_lbl)
        asl.addStretch()
        ais_dismiss = QPushButton("✕")
        ais_dismiss.setFixedSize(18, 18)
        ais_dismiss.setStyleSheet(
            "QPushButton{background:transparent;border:none;"
            "color:#92400E;font-size:10px;}"
            "QPushButton:hover{color:#78350F;}")
        ais_dismiss.clicked.connect(lambda: self.ais_status_bar.setVisible(False))
        asl.addWidget(ais_dismiss)

        # Wrap bar + status into a column so status sits just below the bar
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(bar)
        col.addWidget(self.ais_status_bar)
        return container

    def _mk_footer(self):
        footer = QFrame()
        footer.setFixedHeight(190)
        footer.setStyleSheet("QFrame{background:#0F172A;}")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        log_hdr = QFrame()
        log_hdr.setFixedHeight(32)
        log_hdr.setStyleSheet("QFrame{background:#1E293B;}")
        hhl = QHBoxLayout(log_hdr); hhl.setContentsMargins(16, 0, 12, 0)
        dot = QLabel("●")
        dot.setStyleSheet("color:#22C55E; font-size:9px; margin-right:4px;")
        hhl.addWidget(dot)
        hhl.addWidget(_lbl("LIVE LOGS", 10, bold=True, color="#64748B"))
        hhl.addStretch()
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedHeight(22)
        copy_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{_t().text_muted};border:1px solid {_t().border};"
            f"border-radius:4px;padding:0 10px;font-size:10px;}}"
            f"QPushButton:hover{{color:{_t().text_primary};border-color:{_t().text_muted};}}")
        copy_btn.clicked.connect(self.copy_logs_to_clipboard)
        hhl.addWidget(copy_btn)
        fl.addWidget(log_hdr)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "QTextEdit{background:#0F172A;border:none;"
            f"font-family:'{_MONO_FONT}',monospace;"
            "font-size:11px;color:#7DD3FC;padding:8px 16px;}")
        fl.addWidget(self.log_box)
        return footer

    # ── Grid ──────────────────────────────────────────────────────────────────

    def _apply_row_style(self, row_idx, selected, index=0):
        t  = get_theme(getattr(self, "_current_theme", "light"))
        bg = t.bg_table if index % 2 == 0 else t.bg_table_alt
        fg = t.row_selected_fg if selected else t.text_primary
        
        # Apply style to all items in the row
        for col in range(self.client_table.columnCount()):
            item = self.client_table.item(row_idx, col)
            if item:
                item.setBackground(QColor(bg))
                item.setForeground(QColor(fg))
                # Set font
                font = item.font()
                # PAN (col 2) is bold by default, or bold if selected
                font.setBold(col == 2 or selected)
                item.setFont(font)
        
        cb_container = self.client_table.cellWidget(row_idx, self._TC_CHK)
        if cb_container:
            cb_container.setStyleSheet(f"background:{bg}; border:none;")
            cb = cb_container.findChild(QCheckBox)
            if cb:
                cb.setStyleSheet("background:transparent;")

        # _TC_ACTS is a plain QTableWidgetItem — styled via item background/foreground above

    def _apply_filter(self, text=""):
        if not hasattr(self, "client_table"):
            return
        q = text.strip().lower()
        sf_idx = self.status_filter.currentIndex() if hasattr(self, "status_filter") else 0
        # Map dropdown index → status prefix(es) to match
        _sf_map = {
            0: None,              # All statuses
            1: ("✅",),           # Downloaded
            2: ("⚠",),           # Partially Completed
            3: ("❌",),           # Failed
            4: ("🕐", "⏹"),      # Queued / Pending
            5: ("—",),            # Not run yet
        }
        status_prefixes = _sf_map.get(sf_idx)
        for row_idx in range(self.client_table.rowCount()):
            name_item   = self.client_table.item(row_idx, self._TC_NAME)
            pan_item    = self.client_table.item(row_idx, self._TC_PAN)
            status_item = self.client_table.item(row_idx, self._TC_STATUS)
            if not name_item or not pan_item:
                continue
            st_text = status_item.text().lower() if status_item else ""
            text_match = (not q
                          or q in name_item.text().lower()
                          or q in pan_item.text().lower()
                          or q in st_text)
            if status_prefixes is None:
                status_match = True
            else:
                st = (status_item.text() if status_item else "—")
                status_match = any(st.startswith(p) for p in status_prefixes)
            hidden = not (text_match and status_match)
            self.client_table.setRowHidden(row_idx, hidden)
            if not hidden:
                a_id = name_item.data(Qt.ItemDataRole.UserRole)
                if a_id:
                    is_selected = a_id in self.selected_ids
                    cb_container = self.client_table.cellWidget(row_idx, self._TC_CHK)
                    if cb_container:
                        cb = cb_container.findChild(QCheckBox)
                        if cb and cb.isChecked() != is_selected:
                            cb.blockSignals(True)
                            cb.setChecked(is_selected)
                            cb.blockSignals(False)
                    self._apply_row_style(row_idx, is_selected, row_idx)
        self._update_count()

    def refresh_grid(self):
        self._checkbox_map.clear()
        self._id_to_row.clear()
        self.assessee_list = self.vault.get_all_assessees()
        
        if not hasattr(self, "client_table"):
            return
            
        # Block signals on header_cb during refresh
        if hasattr(self, "header_cb"):
            self.header_cb.blockSignals(True)
            self.header_cb.setEnabled(False)
            
        self.client_table.setRowCount(0)
        
        if not self.assessee_list:
            if hasattr(self, "header_cb"):
                self.header_cb.setChecked(False)
                self.header_cb.blockSignals(False)
            self._update_count()
            return

        if hasattr(self, "header_cb"):
            self.header_cb.setEnabled(True)
            
        # Load download history for the first checked AY — summarized across
        # doc types (a batch can now cover several per client/AY), worst
        # status wins for the glyph shown, full breakdown goes in the tooltip.
        # (F-14: the combo can have multiple years checked; the grid preview
        # only shows one at a time, so it uses whichever is checked first.)
        _checked_ays = self.ay_combo.checked_labels() if hasattr(self, "ay_combo") else []
        current_ay = _checked_ays[0] if _checked_ays else ""
        dl_history = {}
        if current_ay and current_ay != "Select AY/TY":
            try:
                dl_history = self.vault.get_download_history_summary(current_ay)
            except Exception:
                pass

        for i, a in enumerate(self.assessee_list):
            a_id = a.get("id")
            pan  = a.get("pan", "")
            is_selected = a_id in self.selected_ids

            self.client_table.insertRow(i)

            # Col 0: Checkbox
            _ct = _t()
            _chk_path = self.checkmark_path.replace("\\", "/")
            cb_container = QWidget()
            cb_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            cb_container.setStyleSheet(f"QWidget{{background:{_ct.bg_table};}}")
            cb_layout = QHBoxLayout(cb_container)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb = QCheckBox()
            cb.setStyleSheet(
                f"QCheckBox{{background:transparent;}}"
                f"QCheckBox::indicator{{width:15px;height:15px;border:1.5px solid {_ct.border};"
                f"border-radius:3px;background:{_ct.bg_checkbox};}}"
                f"QCheckBox::indicator:hover{{border-color:{_ct.border_focus};}}"
                f"QCheckBox::indicator:checked{{background:{_ct.accent};border-color:{_ct.accent};"
                f"image:url('{_chk_path}');}}"
            )
            cb.setChecked(is_selected)
            cb.toggled.connect(lambda checked, id_=a_id: self._on_check(id_, checked))
            self._checkbox_map[a_id] = cb
            self._id_to_row[a_id] = i
            cb_layout.addWidget(cb)
            self.client_table.setCellWidget(i, self._TC_CHK, cb_container)

            # Col 1: Name
            name_item = QTableWidgetItem(a.get("name", ""))
            name_item.setData(Qt.ItemDataRole.UserRole, a_id)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.client_table.setItem(i, self._TC_NAME, name_item)

            # Col 2: PAN (monospace)
            pan_item = QTableWidgetItem(pan)
            pan_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            f = pan_item.font(); f.setFamily(_MONO_FONT); pan_item.setFont(f)
            self.client_table.setItem(i, self._TC_PAN, pan_item)

            # Col 3: Date of Birth
            dob_item = QTableWidgetItem(a.get("dob", ""))
            dob_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.client_table.setItem(i, self._TC_DOB, dob_item)

            # Col 4: Last Download Status (from history) — one glyph summarizing
            # the worst outcome across doc types, tooltip breaks down each one.
            hist = dl_history.get(pan, {})
            status_text = hist.get("status", "—")
            breakdown = hist.get("breakdown", [])
            status_item = QTableWidgetItem(status_text)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            if len(breakdown) > 1:
                _doc_labels = {"26as": "26AS/Form 168", "request_ais": "AIS Request",
                               "ais_tis": "AIS/TIS", "filed_returns": "Filed Returns",
                               "challans": "Tax Challans", "legacy": "Download"}
                tooltip_text = "\n".join(
                    f"{_doc_labels.get(dt, dt)}: {st}" for dt, st in breakdown
                )
            else:
                tooltip_text = status_text
            status_item.setToolTip(tooltip_text)
            if status_text.startswith("✅"):
                status_item.setForeground(QColor("#15803D"))
            elif status_text.startswith("❌"):
                status_item.setForeground(QColor("#DC2626"))
            elif status_text.startswith("⚠"):
                status_item.setForeground(QColor("#D97706"))
            else:
                status_item.setForeground(QColor("#64748B"))
            self.client_table.setItem(i, self._TC_STATUS, status_item)

            # Col 5: Last Download Time
            ts_text = hist.get("ts", "")
            ts_item = QTableWidgetItem(ts_text if ts_text else "—")
            ts_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ts_item.setForeground(QColor("#64748B"))
            self.client_table.setItem(i, self._TC_TS, ts_item)

            # Col 6: Last Saved Location (hyperlink QLabel)
            saved_path = hist.get("path", "")
            path_lbl = QLabel()
            path_lbl.setContentsMargins(6, 0, 6, 0)
            path_lbl.setStyleSheet("background:transparent; border:none; font-size:11px;")
            if saved_path and os.path.exists(saved_path):
                path_lbl.setText(
                    f'<a href="{saved_path}" style="color:#1D4ED8;text-decoration:underline;">'
                    f'{saved_path}</a>'
                )
                path_lbl.setToolTip(saved_path)
                path_lbl.linkActivated.connect(
                    lambda p=saved_path: self._open_saved_path(p))
            else:
                path_lbl.setText('<span style="color:#94A3B8;">—</span>')
            self.client_table.setCellWidget(i, self._TC_PATH, path_lbl)

            # Col 6: Actions
            dots_item = QTableWidgetItem("• • •")
            dots_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dots_item.setToolTip("Click for actions")
            dots_item.setForeground(QColor("#64748B"))
            f = dots_item.font()
            f.setPointSize(9)
            f.setBold(True)
            dots_item.setFont(f)
            dots_item.setData(Qt.ItemDataRole.UserRole + 1, a)
            self.client_table.setItem(i, self._TC_ACTS, dots_item)

            self._apply_row_style(i, is_selected, i)

        # Re-apply active sort if any
        if self._current_sort_col in (self._TC_NAME, self._TC_PAN):
            self.client_table.setSortingEnabled(True)
            self.client_table.sortByColumn(self._current_sort_col, self._current_sort_order)
            self.client_table.setSortingEnabled(False)

            for row_idx in range(self.client_table.rowCount()):
                item = self.client_table.item(row_idx, self._TC_NAME)
                if item:
                    row_id = item.data(Qt.ItemDataRole.UserRole)
                    row_selected = row_id in self.selected_ids
                    self._apply_row_style(row_idx, row_selected, row_idx)
                    
        if hasattr(self, "header_cb"):
            self.header_cb.blockSignals(False)
            
        self._update_count()
        if hasattr(self, "search_box"):
            self._apply_filter(self.search_box.text())

    def _on_check(self, id_, checked):
        if checked:
            self.selected_ids.add(id_)
        else:
            self.selected_ids.discard(id_)
            
        # Find the row in table widget and apply selection style
        if hasattr(self, "client_table"):
            for row_idx in range(self.client_table.rowCount()):
                item = self.client_table.item(row_idx, self._TC_NAME)
                if item and item.data(Qt.ItemDataRole.UserRole) == id_:
                    self._apply_row_style(row_idx, checked, row_idx)
                    break
                    
        self._update_count()

    def toggle_select_all(self, checked):
        for a_id, cb in self._checkbox_map.items():
            row_idx = self._id_to_row.get(a_id)
            if row_idx is None or self.client_table.isRowHidden(row_idx):
                continue
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
            if checked:
                self.selected_ids.add(a_id)
            else:
                self.selected_ids.discard(a_id)
            self._apply_row_style(row_idx, checked, row_idx)
        self._update_count()

    def _update_count(self):
        visible_ids = {
            a_id for a_id, row in self._id_to_row.items()
            if not self.client_table.isRowHidden(row)
        } if hasattr(self, "client_table") else set(self._checkbox_map)
        n = len(self.selected_ids & visible_ids)
        total = len(visible_ids)
        self.lbl_selected.setText(f"{n} selected" if n else "")
        if hasattr(self, "header_cb"):
            self.header_cb.blockSignals(True)
            self.header_cb.setChecked(total > 0 and n == total)
            self.header_cb.blockSignals(False)


    # ── Form Operations ───────────────────────────────────────────────────────

    # ── Client Dialog (Add / Edit popup) ─────────────────────────────────────

    def _open_add_client(self):
        self._client_dialog(None)

    def _open_edit_client(self, a):
        self._client_dialog(a)

    def _pick_and_edit_client(self):
        """Client Master → Edit Client… — picker popup then edit dialog."""
        clients = [{"id": a["id"], "name": a["name"], "pan": a["pan"]}
                   for a in self.vault.get_all_assessees()]
        if not clients:
            QMessageBox.information(self, "No Clients", "No clients in vault.")
            return
        dlg = _ClientPickerDialog(self, clients, mode="edit")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_ids:
            a_id = dlg.selected_ids[0]
            for row in range(self.client_table.rowCount()):
                item = self.client_table.item(row, self._TC_NAME)
                if item and item.data(Qt.ItemDataRole.UserRole) == a_id:
                    acts_item = self.client_table.item(row, self._TC_ACTS)
                    if acts_item:
                        a = acts_item.data(Qt.ItemDataRole.UserRole + 1)
                        if a:
                            self._open_edit_client(a)
                    break

    def _pick_and_delete_clients(self):
        """Client Master → Delete Client(s)… — picker popup then confirmed delete."""
        all_clients = self.vault.get_all_assessees()
        clients = [{"id": a["id"], "name": a["name"], "pan": a["pan"]}
                   for a in all_clients]
        if not clients:
            QMessageBox.information(self, "No Clients", "No clients in vault.")
            return
        dlg = _ClientPickerDialog(self, clients, mode="delete")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_ids:
            ids   = set(dlg.selected_ids)
            names = [a["name"] for a in all_clients if a["id"] in ids]
            msg   = f"Permanently delete {len(ids)} client(s)?\n\n" + "\n".join(f"\u2022 {n}" for n in names)
            if QMessageBox.question(
                    self, "Confirm Delete", msg,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
               ) == QMessageBox.StandardButton.Yes:
                for a_id in ids:
                    self.vault.delete_assessee(a_id)
                    self.selected_ids.discard(a_id)
                    self._checkbox_map.pop(a_id, None)
                self.refresh_grid()
                self._update_count()
                self.log(f"[Vault] {len(ids)} client(s) deleted.")


    def _client_dialog(self, a=None):
        editing = a is not None
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Client" if editing else "Add New Client")
        dlg.setFixedWidth(400)
        t = _t()
        dlg.setStyleSheet(
            f"QDialog{{background:{t.bg_window};}}"
            f"QLabel{{background:transparent;border:none;color:{t.text_primary};font-size:12px;}}"
            f"QLineEdit{{border:1px solid {t.border};border-radius:6px;padding:6px 10px;"
            f"font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
            f"QLineEdit:focus{{border-color:{t.border_focus};background:{t.bg_input_focus};}}"
        )
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(28, 24, 28, 24)
        vl.setSpacing(0)

        title_lbl = QLabel("Edit Client" if editing else "Add New Client")
        title_lbl.setStyleSheet(f"font-size:16px;font-weight:700;color:{t.text_primary};")
        vl.addWidget(title_lbl)
        vl.addSpacing(18)

        fields = {}

        def _field_label(text):
            l = QLabel(text)
            l.setStyleSheet(f"font-size:11px;font-weight:600;color:{t.text_muted};margin-bottom:3px;")
            return l

        # ── Name ──────────────────────────────────────────────────────────────
        vl.addWidget(_field_label("Full Name"))
        fields["name"] = QLineEdit()
        fields["name"].setPlaceholderText("e.g. Deepak Bholusaria")
        fields["name"].setFixedHeight(34)
        fields["name"].setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        vl.addWidget(fields["name"])
        vl.addSpacing(10)

        # ── PAN ───────────────────────────────────────────────────────────────
        vl.addWidget(_field_label("PAN Number"))
        fields["pan"] = QLineEdit()
        fields["pan"].setPlaceholderText("e.g. ABCDE1234F")
        fields["pan"].setFixedHeight(34)
        fields["pan"].setMaxLength(10)
        fields["pan"].setValidator(
            QRegularExpressionValidator(QRegularExpression("[A-Za-z0-9]{0,10}")))
        fields["pan"].setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        fields["pan"].textChanged.connect(
            lambda txt, e=fields["pan"]: e.setText(txt.upper()) if txt != txt.upper() else None)
        vl.addWidget(fields["pan"])
        vl.addSpacing(10)

        # ── Date of Birth — text input + calendar picker + live hint ─────────
        vl.addWidget(_field_label("Date of Birth"))
        dob_row = QHBoxLayout()
        dob_row.setSpacing(6)
        dob_row.setContentsMargins(0, 0, 0, 0)

        dob_edit = QLineEdit()
        dob_edit.setPlaceholderText("DD-MM-YYYY  or type digits freely")
        dob_edit.setFixedHeight(34)
        dob_edit.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        fields["dob"] = dob_edit

        cal_btn = QPushButton("📅")
        cal_btn.setFixedSize(34, 34)
        cal_btn.setToolTip("Pick date from calendar")
        cal_btn.setStyleSheet(
            f"QPushButton{{background:{t.bg_input};border:1px solid {t.border};"
            f"border-radius:6px;font-size:15px;}}"
            f"QPushButton:hover{{border-color:{t.border_focus};}}")

        dob_row.addWidget(dob_edit, 1)
        dob_row.addWidget(cal_btn)
        vl.addLayout(dob_row)

        # Hint label: shows normalised date or error inline
        dob_hint = QLabel("")
        dob_hint.setStyleSheet("font-size:10px;background:transparent;border:none;")
        dob_hint.setFixedHeight(16)
        vl.addWidget(dob_hint)
        vl.addSpacing(6)

        def _smart_normalise(text: str) -> str:
            """Pre-process raw text before vault normalisation.
            Handles pure digit strings: 8 digits → DD-MM-YYYY, 6 digits → DD-MM-YY.
            """
            import re as _re
            from vault import _normalise_dob
            s = text.strip()
            # Pure digits only — insert dashes
            if _re.fullmatch(r"\d{8}", s):          # 12121975 → 12-12-1975
                s = f"{s[:2]}-{s[2:4]}-{s[4:]}"
            elif _re.fullmatch(r"\d{6}", s):         # 121275  → 12-12-75
                s = f"{s[:2]}-{s[2:4]}-{s[4:]}"
            return _normalise_dob(s)

        def _normalise_and_hint(text):
            """Show a live ✓/✗ hint as the user types."""
            from vault import _DOB_RE
            raw = text.strip()
            if not raw:
                dob_hint.setText("")
                dob_edit.setStyleSheet(
                    f"QLineEdit{{border:1px solid {t.border};border-radius:6px;"
                    f"padding:6px 10px;font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
                    f"QLineEdit:focus{{border-color:{t.border_focus};}}")
                return
            # Only validate when enough chars typed (avoid red flash on first keystrokes)
            if len(raw) < 6:
                dob_hint.setText("")
                dob_edit.setStyleSheet(
                    f"QLineEdit{{border:1px solid {t.border};border-radius:6px;"
                    f"padding:6px 10px;font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
                    f"QLineEdit:focus{{border-color:{t.border_focus};}}")
                return
            normed = _smart_normalise(raw)
            if _DOB_RE.match(normed):
                dob_hint.setText(f"✓  Will save as: {normed}")
                dob_hint.setStyleSheet("font-size:10px;color:#16A34A;background:transparent;border:none;")
                dob_edit.setStyleSheet(
                    f"QLineEdit{{border:1.5px solid #16A34A;border-radius:6px;"
                    f"padding:6px 10px;font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
                    f"QLineEdit:focus{{border-color:#16A34A;}}")
            else:
                dob_hint.setText("✗  Use DDMMYYYY or DD-MM-YYYY  e.g. 06071974")
                dob_hint.setStyleSheet("font-size:10px;color:#EF4444;background:transparent;border:none;")
                dob_edit.setStyleSheet(
                    f"QLineEdit{{border:1.5px solid #EF4444;border-radius:6px;"
                    f"padding:6px 10px;font-size:12px;background:{t.bg_input};color:{t.text_primary};}}"
                    f"QLineEdit:focus{{border-color:#EF4444;}}")

        def _on_dob_commit():
            """On Tab/Enter/focus-out, rewrite the field to canonical DD-MM-YYYY."""
            from vault import _DOB_RE
            raw = dob_edit.text().strip()
            if not raw:
                return
            normed = _smart_normalise(raw)
            if _DOB_RE.match(normed):
                # Block signal to avoid recursive hint update while rewriting
                dob_edit.blockSignals(True)
                dob_edit.setText(normed)
                dob_edit.blockSignals(False)
                _normalise_and_hint(normed)

        dob_edit.textChanged.connect(_normalise_and_hint)
        dob_edit.editingFinished.connect(_on_dob_commit)

        # Calendar popup
        def _show_calendar():
            from PyQt6.QtCore import QDate
            from PyQt6.QtGui import QPalette, QColor
            from vault import _normalise_dob, _DOB_RE

            popup = QDialog(dlg)
            popup.setWindowTitle("Pick Date of Birth")
            popup.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
            popup.setStyleSheet(
                f"QDialog{{background:{t.bg_panel};border:2px solid {t.border};border-radius:8px;}}"
                f"QToolButton{{color:{t.text_primary};background:transparent;border:none;"
                f"border-radius:4px;padding:3px 8px;font-size:12px;font-weight:600;}}"
                f"QToolButton:hover{{background:{t.accent};color:{t.accent_text};}}"
                f"QToolButton::menu-indicator{{image:none;}}"
                f"QSpinBox{{color:{t.text_primary};background:{t.bg_input};"
                f"border:1px solid {t.border};border-radius:4px;padding:2px 6px;}}"
                f"QSpinBox::up-button,QSpinBox::down-button{{background:transparent;border:none;}}"
                f"QMenu{{background:{t.bg_menu};color:{t.text_primary};border:1.5px solid {t.border_menu};}}"
                f"QMenu::item:selected{{background:{t.accent};color:{t.accent_text};}}"
                f"QHeaderView::section{{background:{t.bg_header};color:{t.text_muted};"
                f"border:none;font-size:11px;font-weight:600;padding:4px 0;}}"
                f"#qt_calendar_navigationbar{{background:{t.bg_header};padding:6px 8px;}}"
                f"QAbstractItemView{{background:{t.bg_table};color:{t.text_primary};"
                f"selection-background-color:{t.accent};selection-color:{t.accent_text};"
                f"outline:none;border:none;}}"
            )

            pl = QVBoxLayout(popup)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(0)

            cal = QCalendarWidget(popup)
            cal.setGridVisible(False)
            cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)

            # Set QPalette on the calendar so Qt's internal painter uses theme colours
            pal = QPalette()
            pal.setColor(QPalette.ColorRole.Window,      QColor(t.bg_table))
            pal.setColor(QPalette.ColorRole.Base,        QColor(t.bg_table))
            pal.setColor(QPalette.ColorRole.AlternateBase, QColor(t.bg_table_alt))
            pal.setColor(QPalette.ColorRole.Text,        QColor(t.text_primary))
            pal.setColor(QPalette.ColorRole.BrightText,  QColor(t.accent_text))
            pal.setColor(QPalette.ColorRole.Highlight,   QColor(t.accent))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor(t.accent_text))
            pal.setColor(QPalette.ColorRole.ButtonText,  QColor(t.text_primary))
            pal.setColor(QPalette.ColorRole.Button,      QColor(t.bg_input))
            pal.setColor(QPalette.ColorRole.WindowText,  QColor(t.text_primary))
            pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(t.bg_menu))
            pal.setColor(QPalette.ColorRole.ToolTipText, QColor(t.text_primary))

            # Disabled role — other-month days
            pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
                         QColor(t.text_muted))
            cal.setPalette(pal)

            # Apply palette to all child widgets (nav bar, table view, header)
            for child in cal.findChildren(QWidget):
                child.setPalette(pal)
                child.setAutoFillBackground(True)

            # Pre-select current value if valid
            raw = dob_edit.text().strip()
            normed = _normalise_dob(raw) if raw else ""
            if _DOB_RE.match(normed):
                try:
                    d, m, y = normed.split("-")
                    cal.setSelectedDate(QDate(int(y), int(m), int(d)))
                except Exception:
                    pass

            cal.setMaximumDate(QDate.currentDate())
            pl.addWidget(cal)

            def _picked(date):
                dob_edit.setText(f"{date.day():02d}-{date.month():02d}-{date.year()}")
                popup.accept()

            cal.clicked.connect(_picked)
            pos = cal_btn.mapToGlobal(cal_btn.rect().bottomLeft())
            popup.move(pos)
            popup.exec()

        cal_btn.clicked.connect(_show_calendar)

        # ── Portal Password ───────────────────────────────────────────────────
        vl.addWidget(_field_label("Portal Password"))
        fields["pwd"] = QLineEdit()
        fields["pwd"].setPlaceholderText("Enter password")
        fields["pwd"].setFixedHeight(34)
        fields["pwd"].setEchoMode(QLineEdit.EchoMode.Password)
        fields["pwd"].setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        vl.addWidget(fields["pwd"])
        vl.addSpacing(10)

        # Show password toggle
        show_cb = QCheckBox("Show password")
        show_cb.setStyleSheet(f"color:{t.text_muted};font-size:11px;background:transparent;")
        show_cb.toggled.connect(
            lambda v: fields["pwd"].setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password))
        vl.addWidget(show_cb)
        vl.addSpacing(14)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{t.border};border:none;max-height:1px;")
        vl.addWidget(sep)
        vl.addSpacing(10)

        # ── Email ─────────────────────────────────────────────────────────────
        vl.addWidget(_field_label("Email Address (optional)"))
        fields["email"] = QLineEdit()
        fields["email"].setPlaceholderText("client@example.com")
        fields["email"].setFixedHeight(34)
        fields["email"].setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        vl.addWidget(fields["email"])
        vl.addSpacing(10)

        # ── CC ────────────────────────────────────────────────────────────────
        vl.addWidget(_field_label("CC (optional — separate multiple with  ;)"))
        fields["cc"] = QLineEdit()
        fields["cc"].setPlaceholderText("spouse@example.com;accountant@firm.com")
        fields["cc"].setFixedHeight(34)
        fields["cc"].setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        vl.addWidget(fields["cc"])
        vl.addSpacing(18)

        # Pre-fill if editing
        if editing:
            fields["name"].setText(a.get("name", ""))
            fields["pan"].setText(a.get("pan", ""))
            fields["dob"].setText(a.get("dob", ""))
            fields["pwd"].setText(a.get("password", ""))
            fields["email"].setText(a.get("email", ""))
            fields["cc"].setText(a.get("cc", ""))

        # Buttons
        btn_row = QHBoxLayout()
        btn_cancel = _btn("Cancel", "secondary", height=34, icon="btn_cancel.png")
        btn_save   = _btn("Update Client" if editing else "Add Client", "primary", height=34, icon="btn_add_client.png")
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_save)
        vl.addLayout(btn_row)

        btn_cancel.clicked.connect(dlg.reject)

        def _save():
            try:
                edit_id = a.get("id") if editing else None
                self.vault.add_update_assessee(
                    fields["name"].text(), fields["pan"].text(),
                    fields["dob"].text(), fields["pwd"].text(), edit_id,
                    email=fields["email"].text(),
                    cc=fields["cc"].text())
                action = "updated" if editing else "added"
                self.log(f"[Vault] Client {action}: {fields['pan'].text()} — {fields['name'].text()}")
                dlg.accept()
                self.refresh_grid()
            except ValueError as ve:
                QMessageBox.critical(dlg, "Validation Error", str(ve))
            except Exception as ex:
                QMessageBox.critical(dlg, "Error", str(ex))

        btn_save.clicked.connect(_save)
        dlg.exec()

    def save_assessee(self):
        self._open_add_client()

    # ── Email menu handlers ───────────────────────────────────────────────────

    def _open_email_settings(self):
        from ui.dialogs import SmtpSettingsDialog
        SmtpSettingsDialog(self, self.vault).exec()

    def _open_mail_docs(self):
        from ui.dialogs import MailDocsDialog
        # F-14: Mail Docs works against one year at a time — uses whichever
        # checked year is first if the combo has more than one checked.
        _checked = self.ay_combo.checked_labels() if hasattr(self, "ay_combo") else []
        ay_label = _checked[0] if _checked else ""
        MailDocsDialog(self, self.vault, ay_label).exec()

    def _show_log_history(self, a: dict):
        history  = self.log_store.get(a.get("pan", ""))
        _checked = self.ay_combo.checked_labels() if hasattr(self, "ay_combo") else []
        ay_label = _checked[0] if _checked else ""
        LogHistoryDialog(self, name=a.get("name", ""), pan=a.get("pan", ""),
                         history=history, active_ay=ay_label).exec()

    def delete_assessee(self, assessee_id):
        if QMessageBox.question(self, "Confirm Delete",
            "Delete this assessee from the vault?") == QMessageBox.StandardButton.Yes:
            try:
                self.vault.delete_assessee(assessee_id)
                self.selected_ids.discard(assessee_id)
                self.log("[Vault] Assessee deleted.")
                self.refresh_grid()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete: {e}")

    def delete_selected(self):
        n = len(self.selected_ids)
        if not n:
            return
        if QMessageBox.question(self, "Confirm Bulk Delete",
                f"Delete {n} selected assessee{'s' if n > 1 else ''} from the vault?\n\nThis cannot be undone."
                ) != QMessageBox.StandardButton.Yes:
            return
        errors = []
        for a_id in list(self.selected_ids):
            try:
                self.vault.delete_assessee(a_id)
            except Exception as e:
                errors.append(str(e))
        self.selected_ids.clear()
        self.log(f"[Vault] {n} assessee{'s' if n > 1 else ''} deleted.")
        if errors:
            QMessageBox.warning(self, "Partial Delete", f"{len(errors)} deletion(s) failed:\n" + "\n".join(errors))
        self.refresh_grid()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_saved_path(self, path: str):
        from config import _open_path, _log_open
        _log_open(f"[OpenFolder] Grid link clicked: {path!r}")
        _open_path(path)

    def _open_output_folder(self):
        from config import _open_path, _log_open
        path = self.dir_lbl.text()
        _log_open(f"[OpenFolder] Main window Open button: {path!r}")
        _open_path(path)

    def browse_output_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select Output Directory",
            self.dir_lbl.text())
        if chosen:
            if sys.platform == "win32":
                chosen = chosen.replace("/", "\\")
            self.dir_lbl.setText(chosen)
            self.vault.update_setting("download_root_dir", chosen)
            self.log(f"[Settings] Output folder: {chosen}")

    def _ay_json_path(self) -> str:
        """
        Writable path for assessment_years.json in the user data dir.

        The bundled copy lists the years shipped with the release; the writable
        copy holds the user's enable/disable choices and any years they added.
        Both are merged on every launch so years introduced by a new release
        show up for existing installs while their edits survive. Seeding only
        when the file was missing left upgraded installs pinned to the year
        list from whenever the app first ran.
        """
        writable = os.path.join(_app_dir(), "assessment_years.json")
        bundled = os.path.join(_bundled_dir(), "assessment_years.json")

        # Running from source: both resolve to the repo file — nothing to merge.
        if os.path.abspath(writable) == os.path.abspath(bundled):
            return writable

        try:
            with open(bundled, "r", encoding="utf-8") as f:
                bundled_entries = json.load(f)
        except Exception:
            return writable

        if not os.path.exists(writable):
            try:
                import shutil
                shutil.copy2(bundled, writable)
            except Exception:
                pass
            return writable

        try:
            with open(writable, "r", encoding="utf-8") as f:
                user_entries = json.load(f)
            known = {e.get("label") for e in user_entries}
            missing = [e for e in bundled_entries if e.get("label") not in known]
            if missing:
                user_entries.extend(missing)
                with open(writable, "w", encoding="utf-8") as f:
                    json.dump(user_entries, f, indent=2)
        except Exception:
            # Runs during UI construction, before the log widget exists — a bad
            # merge must never block startup; the user keeps their existing list.
            pass

        return writable

    def _load_ay_list(self):
        path = self._ay_json_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            # Sort: disabled (future TY) first, then descending by year
            def _sort_key(e):
                y = e.get("year", {})
                label_year = (y.get("AY") or y.get("TY") or y.get("FY") or "0000-00")
                try:
                    return (0 if not e.get("enabled", True) else 1, -int(label_year[:4]))
                except ValueError:
                    return (1, 0)
            return sorted(entries, key=_sort_key)
        except Exception:
            return [
                {"label": "AY 2025-26 (FY 2024-25)", "enabled": True, "year": {"AY": "2025-26", "FY": "2024-25"}},
                {"label": "AY 2024-25 (FY 2023-24)", "enabled": True, "year": {"AY": "2024-25", "FY": "2023-24"}},
            ]

    def _resolve_ay_fy(self, label: str):
        """
        Returns (ay_or_ty_value, fy_value, year_type, form_type) where year_type
        is 'AY' or 'TY'. The form is derived from the year type (see forms.py),
        not read from the year entry, so an installed copy of
        assessment_years.json written by an older release can't disagree with
        the code about which form to fetch.
        """
        for e in self._ay_entries:
            if e["label"] == label:
                y = e["year"]
                if y.get("AY"):
                    return y["AY"], y.get("FY"), "AY", form_for("AY", y["AY"])
                if y.get("TY"):
                    return y["TY"], y.get("FY"), "TY", form_for("TY", y["TY"])
        return None, None, "AY", DEFAULT_FORM

    def open_manage_years(self):
        ManageYearsDialog(self, self._ay_json_path(), on_save=self.refresh_ay_combo).exec()

    def refresh_ay_combo(self):
        self._ay_entries = self._load_ay_list()
        ay_labels = [e["label"] for e in self._ay_entries if e.get("enabled", True)]
        current = set(self.ay_combo.checked_labels())
        self.ay_combo.blockSignals(True)
        self.ay_combo.clear_items()
        for _label in ay_labels:
            self.ay_combo.add_item(_label, checked=(_label in current))
        self.ay_combo.blockSignals(False)
        self.log("[Settings] Assessment Year list refreshed.")

    def _log_ay_selection(self):
        labels = self.ay_combo.checked_labels()
        if labels:
            self.log(f"[Settings] Assessment Year(s) → {', '.join(labels)}")

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, message):
        text = f"[{get_timestamp()}] {message}"
        self._log_signal.emit(text)
        try:
            with open(os.path.join(_app_dir(), "app.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _append_log(self, text):
        self.log_box.append(text)
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)

    def copy_logs_to_clipboard(self):
        QApplication.clipboard().setText(self.log_box.toPlainText())
        self.log("[System] Logs copied to clipboard.")

    # ── Bulk Import ───────────────────────────────────────────────────────────

    def bulk_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import File", "",
            "Excel / CSV files (*.xlsx *.csv)")
        if not path:
            return
        self.log(f"[Vault] Importing {os.path.basename(path)}...")
        added, updated, errors = self.vault.import_bulk(path)
        total = added + updated
        parts = []
        if added:
            parts.append(f"{added} new record{'s' if added != 1 else ''} added")
        if updated:
            parts.append(f"{updated} existing record{'s' if updated != 1 else ''} updated")
        summary = ", ".join(parts) if parts else "No records imported"
        self.log(f"[Vault] Import complete — {summary}.")
        if errors:
            for err in errors:
                self.log(f"  - {err}")
            QMessageBox.warning(self, "Import Complete",
                f"{summary}.\n\n{len(errors)} row(s) had errors — see logs for details.")
        else:
            QMessageBox.information(self, "Import Complete", f"{summary}.")
        self.refresh_grid()

    def generate_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Template",
            "Assessee_Import_Template", "Excel Workbook (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        try:
            self.vault.generate_template(path)
            self.log(f"[Vault] Template generated: {path}")
            QMessageBox.information(self, "Success", f"Template generated at:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed: {e}")

    def export_data(self):
        assessees = self.vault.get_all_assessees()
        if not assessees:
            QMessageBox.information(self, "No Data", "No assessees saved yet.")
            return
        _out_dir = self.vault.get_setting("download_root_dir", "")
        _default = os.path.join(_out_dir, "Assessee_Export") if _out_dir and os.path.isdir(_out_dir) else "Assessee_Export"
        path, _ = QFileDialog.getSaveFileName(self, "Export Saved Data",
            _default, "Excel Workbook (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        if not (path.endswith('.xlsx') or path.endswith('.csv')):
            path += ".xlsx"
        try:
            self.vault.export_data(path)
            self.log(f"[Vault] Data exported: {path}")
            QMessageBox.information(self, "Export Complete",
                f"{len(assessees)} record(s) exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed: {e}")

    # ── Browser health check ──────────────────────────────────────────────────

    def _check_browser(self):
        """Run silently on startup; installs Chromium if missing."""
        def _run():
            import asyncio
            from automation.browser import _playwright_browsers_dir, _install_chromium
            import os
            # Check if chromium executable already exists in our browsers dir
            browsers_dir = _playwright_browsers_dir()
            chromium_exists = any(
                f.startswith("chromium") for f in os.listdir(browsers_dir)
            ) if os.path.exists(browsers_dir) else False
            if not chromium_exists:
                self.log("[Browser] Chromium not found — installing in background...")
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_install_chromium(self.log))
                except Exception as e:
                    self.log(f"[Browser] Auto-install failed: {e}")
                    self.log("[Browser] Run 'playwright install chromium' manually if downloads fail.")
                finally:
                    loop.close()
            else:
                self.log("[Browser] Chromium ready.")
        threading.Thread(target=_run, daemon=True).start()

    def _check_for_update(self, manual=False):
        if manual:
            self._update_check_manual = True
        from updater import check_for_update
        def _cb(tag, _url):
            QMetaObject.invokeMethod(
                self, "_on_update_result",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, tag or ""),
            )
        check_for_update(_cb)

    @pyqtSlot(str)
    def _on_update_result(self, tag: str):
        manual = getattr(self, "_update_check_manual", False)
        self._update_check_manual = False
        if tag:
            # Stop any existing blink timer before starting a new one
            if hasattr(self, "_update_blink_timer"):
                self._update_blink_timer.stop()
            self._update_link_text = f'<a href="#" style="color:#2563EB;font-size:11px;">&#11015; v{tag} available</a>'
            self._update_link_visible = True
            self._hdr_update_lnk.setText(self._update_link_text)
            self._update_blink_timer = QTimer(self)
            def _blink():
                self._update_link_visible = not self._update_link_visible
                self._hdr_update_lnk.setText(
                    self._update_link_text if self._update_link_visible else ""
                )
            self._update_blink_timer.timeout.connect(_blink)
            self._update_blink_timer.start(600)
            if manual:
                from PyQt6.QtWidgets import QMessageBox
                res = QMessageBox.question(
                    self, "Update Available",
                    f"Version {tag} is available.\n\nOpen the download page?",
                )
                if res == QMessageBox.StandardButton.Yes:
                    self._on_update_link_clicked()
        elif manual:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Up to Date",
                f"You're on the latest version (v{APP_VERSION})."
            )

    def _on_update_link_clicked(self):
        if hasattr(self, "_update_blink_timer"):
            self._update_blink_timer.stop()
        self._hdr_update_lnk.setText(getattr(self, "_update_link_text", ""))
        QDesktopServices.openUrl(QUrl("https://download.aaydoccapio.com/"))

    # ── Automation ────────────────────────────────────────────────────────────

    def _lock_ui(self, lock: bool):
        widgets = [self.ay_combo, self.btn_run, self.chk_headless]
        for _act in (getattr(self, "_act_edit_cl", None), getattr(self, "_act_del_cl", None)):
            if _act:
                _act.setEnabled(not lock)
        if hasattr(self, "header_cb"):
            widgets.append(self.header_cb)
        for w in widgets:
            w.setEnabled(not lock)
        # Disable Client Master / Settings menus during batch
        menubar = self.menuBar()
        for action in menubar.actions():
            if action.text() in ("Client Master", "Settings"):
                if action.menu():
                    action.menu().setEnabled(not lock)

    def _open_download_picker(self):
        """F-56 Phase 3 — the Download button opens a checkbox picker instead
        of the old dropdown menu; on confirm, kicks off start_automation with
        whatever combination of document types the user selected."""
        if self.is_running:
            return
        dlg = DownloadPickerDialog(self, self.vault)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.start_automation(dlg.selected_docs)

    def start_automation(self, selected_docs):
        """
        selected_docs: set/collection of doc-type strings to run, any of
        "26as", "request_ais", "ais_tis", "filed_returns" — one batch run
        can now cover several at once (F-56 Phase 2).

        F-14 (multi-year): the AY/TY combo is now a multi-select — every
        checked year runs for every selected client. `year_specs` (built
        below) is the single list threaded through the rest of the batch:
        one dict per checked year, each carrying its own resolved AY/TY
        value, FY, year type, and form (26AS vs Form 168).
        """
        selected_docs = set(selected_docs) if not isinstance(selected_docs, set) else selected_docs
        if self.is_running:
            return
        if not selected_docs:
            return
        if not self.selected_ids:
            QMessageBox.warning(self, "Selection Required",
                "Please select at least one client.")
            return
        ay_labels = self.ay_combo.checked_labels()
        if not ay_labels:
            QMessageBox.warning(self, "Selection Required",
                "Please select at least one Assessment / Tax Year.")
            return

        year_specs = []
        for ay_label in ay_labels:
            ay, fy, year_type, form_type = self._resolve_ay_fy(ay_label)
            if not ay:
                self.log(f"[Warning] Skipping unresolvable year: {ay_label}")
                continue
            year_specs.append({
                "ay_label": ay_label, "value": ay, "fy": fy,
                "year_type": year_type, "form_type": form_type,
            })
        if not year_specs:
            QMessageBox.warning(self, "Invalid", "None of the selected years could be resolved.")
            return

        self.is_running = True
        self._batch_aborted = False
        self._batch_filing_scope = self.vault.get_setting("filed_returns_scope", "all")
        self._lock_ui(True)
        self.log_box.clear()

        targets = [a for a in self.assessee_list if a.get("id") in self.selected_ids]
        output_dir = self.dir_lbl.text()
        self._last_batch_params = (year_specs, output_dir, selected_docs)

        self.btn_run.setText("⏳ Downloading...")

        run_label = " + ".join(DOC_TYPE_LABELS.get(d, d) for d in sorted(selected_docs))
        years_desc = ", ".join(ay_labels)
        self.log(f"[System] Starting {run_label} — {len(targets)} client(s) | Year(s): {years_desc} | Output: {output_dir}")

        # Year tag shown in progress dialog title
        year_tag = years_desc if len(ay_labels) <= 3 else f"{len(ay_labels)} years"

        # Show progress dialog (on main thread via signal)
        self._show_progress_signal.emit(targets, selected_docs, year_specs, output_dir, year_tag)

        # Enable "Send to Tray" in tray menu while batch is running
        if hasattr(self, "_tray_send_act"):
            self._tray_send_act.setVisible(True)

        # F-35: send to system tray if auto-minimise enabled
        if getattr(self, "chk_auto_minimise", None) and self.chk_auto_minimise.isChecked():
            QTimer.singleShot(800, lambda: self._tray_to_system(len(targets)))

        threading.Thread(
            target=self._run_wrapper,
            args=(targets, year_specs, output_dir, selected_docs),
            daemon=True).start()

    def _show_progress_dialog(self, targets: list, selected_docs: set, year_specs: list,
                               output_dir: str = "", year_tag: str = ""):
        """Called on main thread to create and show the progress dialog."""
        self._progress_dialog = BatchProgressDialog(
            targets, selected_docs, year_specs=year_specs, ay=year_tag,
            stop_callback=self.stop_automation,
            resume_callback=self.resume_batch, skip_callback=self.skip_client,
            tray_callback=self._tray_to_system_manual,
            output_dir=output_dir, parent=self)
        # Window-modal: blocks the parent window but allows live signal updates.
        self._progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dialog.show()
        self._progress_dialog.raise_()
        self._progress_dialog.activateWindow()

    # ── F-64: Generate Tax Challans ──────────────────────────────────────────

    def _open_generate_challans_dialog(self):
        """Toolbar/menu entry point for bulk challan generation — a separate
        flow from the Downloads batch above, since each row here carries its
        own amount breakup (not just client identity), which the existing
        HANDLERS/DownloadPickerDialog/BatchProgressDialog pipeline has no
        room for. See PlansofThisProject/F-64_bulk_tax_challan_generation.md."""
        if self._challan_running:
            return
        dlg = GenerateChallansDialog(self, self.vault, self._ay_entries)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.start_challan_generation(dlg.fy_value, dlg.rows)

    def _download_challan_template(self):
        """E-Pay Tax > Download Import Template — lets a user get the blank
        template straight away, without opening the full Generate Tax
        Challans dialog first just to reach its own template button."""
        from ui.dialogs import download_challan_template
        path, _ = QFileDialog.getSaveFileName(self, "Save Template",
            "Challan_Rows_Template", "Excel Workbook (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        try:
            download_challan_template(path)
            QMessageBox.information(self, "Success", f"Template generated at:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed: {e}")

    def start_challan_generation(self, fy_value: str, rows: list):
        from automation.challan_generator import resolve_tax_type, TAX_TYPES
        try:
            tax_type, portal_year_label = resolve_tax_type(fy_value, self._ay_entries)
        except Exception as e:
            QMessageBox.critical(self, "Year Not Ready", str(e))
            return

        # Match each row's PAN against the vault once, up front, so a client
        # removed/renamed between dialog-open and Generate-click can't crash
        # the batch mid-run — same defensiveness as start_automation's
        # targets list build.
        by_pan = {a.get("pan", "").upper(): a for a in self.assessee_list}
        targets = []
        for row in rows:
            client = by_pan.get(row["pan"].upper())
            if not client:
                self.log(f"[Warning] Skipping {row['pan']} — no longer in Client Master.")
                continue
            targets.append({**client, "_amounts": row})
        if not targets:
            QMessageBox.warning(self, "Nothing to Generate", "None of the selected clients could be matched.")
            return

        self._challan_running = True
        self._challan_aborted = False
        self.log_box.clear()
        if hasattr(self, "_tray_send_act"):
            self._tray_send_act.setVisible(True)

        output_dir = self.dir_lbl.text()
        type_label = TAX_TYPES[tax_type]["label"]
        # portal_year_label alone is just a bare "2026-27" — prefix with
        # AY/TY so it's clear which Act/year convention it is, same as the
        # dialog's own combo already shows (e.g. "TY 2026-27 (FY 2026-27)").
        year_prefix = "TY" if tax_type == "advance" else "AY"
        year_display = f"{year_prefix} {portal_year_label}"
        self.log(f"[System] Starting Tax Challan generation — {len(targets)} client(s) | "
                 f"FY {fy_value} → {type_label} | Output: {output_dir}")

        self._show_challan_progress_signal.emit(targets, year_display, type_label, output_dir)

        threading.Thread(
            target=self._run_challan_wrapper,
            args=(targets, fy_value, portal_year_label, tax_type, output_dir),
            daemon=True).start()

    def _show_challan_progress_dialog(self, targets: list, year_display: str, type_label: str, output_dir: str):
        self._challan_progress_dialog = ChallanGenerationProgressDialog(
            targets, year_display, type_label,
            stop_callback=self.stop_challan_generation,
            tray_callback=self._tray_to_system_manual,
            output_dir=output_dir, parent=self)
        self._challan_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._challan_progress_dialog.show()
        self._challan_progress_dialog.raise_()
        self._challan_progress_dialog.activateWindow()

    def stop_challan_generation(self):
        if not self._challan_running:
            return
        self.log("[System] Abort requested (Tax Challan generation)...")
        self._challan_running = False
        self._challan_aborted = True
        if self._challan_task and self._challan_loop:
            self._challan_loop.call_soon_threadsafe(self._challan_task.cancel)

    def _run_challan_wrapper(self, targets, fy_value, portal_year_label, tax_type, output_dir):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._challan_loop = loop
        try:
            self._challan_task = loop.create_task(
                self._execute_challan_generation(targets, fy_value, portal_year_label, tax_type, output_dir))
            loop.run_until_complete(self._challan_task)
        except asyncio.CancelledError:
            self.log("[System] Tax Challan generation cancelled.")
        except Exception as e:
            self.log(f"[System Error] Tax Challan generation crashed: {e}")
        finally:
            self._challan_task = None
            self._challan_loop = None
            loop.close()
            self._challan_running = False
            if self._challan_progress_dialog:
                self._challan_progress_dialog.batch_finished()
            self._challan_done_signal.emit()

    async def _execute_challan_generation(self, targets, fy_value, portal_year_label, tax_type, output_dir):
        from automation.challan_generator import generate_challan, TAX_TYPES

        def set_status(row_index, text):
            if self._challan_progress_dialog:
                self._challan_progress_dialog.set_status(row_index, text)

        try:
            interactive = not self.chk_headless.isChecked()
            context = await browser_manager.get_context(log_callback=self.log, interactive=interactive)
        except Exception as e:
            self.log(f"[System Error] Browser init failed: {e}"); return

        results = []
        processed_indices = set()
        try:
            for i, target in enumerate(targets):
                if not self._challan_running:
                    self.log("[System] Aborted."); break

                pan = target.get("pan", "")
                name = target.get("name", "")
                dob = target.get("dob", "")
                row = target.get("_amounts", {})
                payment_mode = row.get("payment_mode", "")
                bank = row.get("bank", "")
                drawee_bank = row.get("drawee_bank", "")
                amounts = {k: row.get(k, 0) for k in
                           ("tax", "surcharge", "cess", "interest", "penalty", "others")}
                self.log("──────────────────────────────────────────────────")
                self.log(f"[{i+1}/{len(targets)}] {name}")
                set_status(i, "⏳ Logging in to ITD...")

                page = None
                try:
                    page = await login_itd(pan, target.get("password"), self.log, context,
                                            is_running=lambda: self._challan_running)
                    set_status(i, "⏳ Navigating to e-Pay Tax...")
                    from automation.downloader_challans import navigate_to_epay_tax_act
                    await navigate_to_epay_tax_act(page, self.log, TAX_TYPES[tax_type]["act_year_type"])

                    set_status(i, "⏳ Generating challan...")
                    result = await generate_challan(
                        page, fy_value, portal_year_label, tax_type, amounts,
                        payment_mode, bank, drawee_bank,
                        output_dir, self.log, pan=pan, dob=dob)
                    result["pan"] = pan
                    result["name"] = name
                    results.append(result)
                    processed_indices.add(i)

                    if result["status"] == "generated":
                        set_status(i, f"✅ Generated — CRN {result['crn']}")
                        if self._challan_progress_dialog and result.get("artifact_path"):
                            self._challan_progress_dialog.set_artifact_path(i, result["artifact_path"])
                    elif result["status"] == "unavailable":
                        set_status(i, f"⚠ Unavailable — {result['reason']}")
                    else:
                        set_status(i, f"❌ Failed — {result['reason']}")
                except Exception as e:
                    self.log(f"[Error] {name}: {e}")
                    set_status(i, f"❌ Failed — {e}")
                    results.append({
                        "pan": pan, "name": name, "fy_value": fy_value,
                        "portal_year_label": portal_year_label, "tax_type": tax_type,
                        "crn": "", "total_amount": sum(amounts.get(k, 0) or 0 for k in
                            ("tax", "surcharge", "cess", "interest", "penalty", "others")),
                        "valid_till": "", "payment_mode": payment_mode, "bank": bank,
                        "drawee_bank": drawee_bank,
                        "status": "failed", "reason": str(e), "artifact_path": "",
                    })
                    processed_indices.add(i)
                finally:
                    if page:
                        try:
                            await logout_itd(page, self.log)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            # stop_challan_generation() cancels this task directly —
            # CancelledError isn't an Exception subclass (Python 3.8+), so
            # the per-client `except Exception` above never sees it and the
            # in-flight client's row was otherwise left frozen on whatever
            # status it last had ("Logging in...", etc.), making the
            # progress dialog look stuck even though the run did stop.
            self.log("[System] Tax Challan generation stopped by user.")
        finally:
            # Mark the in-flight client (if any) and every not-yet-started
            # one as stopped, rather than leaving their rows on a stale
            # "Logging in..."/"Waiting" status forever. Tracked by row
            # index, not PAN — the same PAN can appear in multiple rows
            # (e.g. a split Cash + Cheque challan pair for one client).
            for idx in range(len(targets)):
                if idx not in processed_indices:
                    set_status(idx, "⏹ Stopped")
            try:
                report_path = self._write_challan_summary(results, output_dir, fy_value, tax_type)
                if report_path and self._challan_progress_dialog:
                    self._challan_progress_dialog.set_report_path(report_path)
            except Exception as e:
                self.log(f"[Warning] Could not write challan summary: {e}")

    def _write_challan_summary(self, results: list, output_dir: str, fy_value: str, tax_type: str) -> str:
        from automation.challan_fields import CHALLAN_SUMMARY_COLUMNS
        from automation.challan_generator import TAX_TYPES
        from openpyxl import Workbook
        import datetime as _dt

        os.makedirs(output_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(output_dir, f"Tax_Challans_Summary_{ts}.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.append(CHALLAN_SUMMARY_COLUMNS)
        # BUG FIX (2026-09-02): "Portal Year Label" showed a bare "2025-26"
        # with no way to tell AY vs TY from the sheet alone — same issue
        # fixed in generate_challan()'s artifact filenames. Prefix it the
        # same way, using TAX_TYPES' act_year_type ("AY" or "TY").
        year_prefix = TAX_TYPES.get(tax_type, {}).get("act_year_type", "")
        for r in results:
            portal_year_label = r.get("portal_year_label", "")
            year_label_display = f"{year_prefix} {portal_year_label}".strip() if portal_year_label else ""
            ws.append([
                r.get("pan", ""), r.get("name", ""), fy_value,
                TAX_TYPES.get(tax_type, {}).get("label", tax_type),
                year_label_display, r.get("payment_mode", ""), r.get("bank", ""),
                r.get("drawee_bank", ""), r.get("total_amount", 0), r.get("crn", ""),
                r.get("valid_till", ""), r.get("status", ""), r.get("reason", ""),
                r.get("artifact_path", ""),
            ])
        wb.save(path)
        self.log(f"[System] Challan summary written: {path}")
        return path

    def _on_challan_batch_done(self):
        self.log("[System] Tax Challan generation idle.")
        if hasattr(self, "_tray_send_act") and not self.is_running:
            self._tray_send_act.setVisible(False)
        self.refresh_grid()

    # ── F-67: ITR Processing Status ──────────────────────────────────────────

    def _open_return_status_dialog(self):
        """Menu entry point — opens the browse/filter/select window. The
        dialog itself calls back into start_return_status_check() below
        when the user actually wants to re-check something; it never runs
        Playwright itself."""
        ReturnStatusDialog(self, self.vault, self._ay_entries).exec()

    def start_return_status_check(self, ay_value: str, targets: list, on_done=None):
        """Called by ReturnStatusDialog with the clients/AY the user picked
        and checked. `on_done` is a plain callback (not a Qt signal) —
        _on_return_status_batch_done invokes it once the batch actually
        finishes, safely on the main thread, since that method is itself
        only ever reached via _return_status_done_signal."""
        if not targets:
            return
        if self._retstatus_running:
            QMessageBox.information(self, "Already Running",
                                     "A status check is already in progress.")
            return
        self._retstatus_running = True
        self._retstatus_aborted = False
        self._retstatus_on_done = on_done
        self.log(f"[System] Starting ITR Processing Status check — {len(targets)} client(s) | AY {ay_value}")
        self._show_return_status_progress_signal.emit(targets, ay_value)
        threading.Thread(
            target=self._run_return_status_wrapper,
            args=(targets, ay_value), daemon=True).start()

    def _show_return_status_progress_dialog(self, targets: list, ay_value: str):
        self._retstatus_progress_dialog = ReturnStatusProgressDialog(
            targets, ay_value, stop_callback=self.stop_return_status_check, parent=self)
        self._retstatus_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._retstatus_progress_dialog.show()
        self._retstatus_progress_dialog.raise_()
        self._retstatus_progress_dialog.activateWindow()

    def stop_return_status_check(self):
        if not self._retstatus_running:
            return
        self.log("[System] Abort requested (ITR Processing Status check)...")
        self._retstatus_running = False
        self._retstatus_aborted = True
        if self._retstatus_task and self._retstatus_loop:
            self._retstatus_loop.call_soon_threadsafe(self._retstatus_task.cancel)

    def _run_return_status_wrapper(self, targets, ay_value):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._retstatus_loop = loop
        try:
            self._retstatus_task = loop.create_task(
                self._execute_return_status_check(targets, ay_value))
            loop.run_until_complete(self._retstatus_task)
        except asyncio.CancelledError:
            self.log("[System] ITR Processing Status check cancelled.")
        except Exception as e:
            self.log(f"[System Error] ITR Processing Status check crashed: {e}")
        finally:
            self._retstatus_task = None
            self._retstatus_loop = None
            loop.close()
            self._retstatus_running = False
            if self._retstatus_progress_dialog:
                self._retstatus_progress_dialog.batch_finished()
            self._return_status_done_signal.emit()

    async def _execute_return_status_check(self, targets, ay_value):
        from automation.return_status import check_return_status

        def set_status(row_index, text):
            if self._retstatus_progress_dialog:
                self._retstatus_progress_dialog.set_status(row_index, text)

        try:
            interactive = not self.chk_headless.isChecked()
            context = await browser_manager.get_context(log_callback=self.log, interactive=interactive)
        except Exception as e:
            self.log(f"[System Error] Browser init failed: {e}"); return

        processed_indices = set()
        try:
            for i, target in enumerate(targets):
                if not self._retstatus_running:
                    self.log("[System] Aborted."); break

                pan = target.get("pan", "")
                name = target.get("name", "")
                dob = target.get("dob", "")
                self.log("──────────────────────────────────────────────────")
                self.log(f"[{i+1}/{len(targets)}] {name}")
                set_status(i, "⏳ Logging in to ITD...")

                page = None
                try:
                    page = await login_itd(pan, target.get("password"), self.log, context,
                                            is_running=lambda: self._retstatus_running)
                    set_status(i, "⏳ Checking status...")
                    result = await check_return_status(page, ay_value, self.log, pan=pan, dob=dob)
                    processed_indices.add(i)

                    if result["ok"]:
                        self.vault.record_return_status(
                            pan, ay_value, result["status"],
                            status_date=result.get("status_date", ""),
                            filing_date=result.get("filing_date", ""),
                            ack_no=result.get("ack_no", ""))
                        status_display = result["status"]
                        if result.get("status_date"):
                            status_display += f" ({result['status_date']})"
                        set_status(i, f"✅ {status_display}")
                    else:
                        set_status(i, f"❌ {result['reason']}")
                except Exception as e:
                    self.log(f"[Error] {name}: {e}")
                    set_status(i, f"❌ Failed — {e}")
                    processed_indices.add(i)
                finally:
                    if page:
                        try:
                            await logout_itd(page, self.log)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            self.log("[System] ITR Processing Status check stopped by user.")
        finally:
            for idx in range(len(targets)):
                if idx not in processed_indices:
                    set_status(idx, "⏹ Stopped")

    def _on_return_status_batch_done(self):
        self.log("[System] ITR Processing Status check idle.")
        cb = self._retstatus_on_done
        self._retstatus_on_done = None
        if cb:
            cb()

    def skip_client(self):
        """Signal the batch runner to skip the currently-downloading client."""
        self._skip_current = True

    def stop_automation(self):
        if not self.is_running:
            return
        _sm = _t()
        _mb = QMessageBox(self)
        _mb.setWindowTitle("Stop")
        _mb.setText("Abort the active batch?")
        _mb.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        _mb.setDefaultButton(QMessageBox.StandardButton.No)
        _mb.setStyleSheet(
            f"QMessageBox{{background:{_sm.bg_window};color:{_sm.text_primary};}}"
            f"QLabel{{color:{_sm.text_primary};background:transparent;}}"
            f"QPushButton{{background:{_sm.accent};color:{_sm.accent_text};border:none;"
            f"border-radius:5px;padding:6px 18px;font-size:13px;}}"
            f"QPushButton:hover{{background:{_sm.accent_hover};}}"
            f"QPushButton[text='No']{{background:{_sm.border};color:{_sm.text_primary};}}"
        )
        if _mb.exec() == QMessageBox.StandardButton.Yes:
            self.log("[System] Abort requested...")
            self.is_running = False
            self._batch_aborted = True
            # Cancel the asyncio task immediately — raises CancelledError into
            # whatever await is currently blocking (goto, wait_for_selector, sleep…)
            if self._batch_task and self._batch_loop:
                self._batch_loop.call_soon_threadsafe(self._batch_task.cancel)

    def resume_batch(self, remaining_targets: list):
        """Called from the dialog Resume button — restart batch with unfinished clients."""
        if not self._last_batch_params or not remaining_targets:
            return
        year_specs, root_dir, selected_docs = self._last_batch_params
        self.is_running = True
        self._batch_aborted = False
        self._lock_ui(True)
        self.btn_run.setText("⏳ Downloading...")
        self.log(f"[System] Resuming — {len(remaining_targets)} client(s) remaining...")
        if self._progress_dialog:
            self._progress_dialog.batch_resumed()
        threading.Thread(
            target=self._run_wrapper,
            args=(remaining_targets, year_specs, root_dir, selected_docs),
            daemon=True).start()

    def _run_wrapper(self, targets, year_specs, root_dir, selected_docs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._batch_loop = loop

        # Suppress "Future exception was never retrieved" noise from Playwright
        # futures that are orphaned when the browser closes mid-await after Stop.
        def _exc_handler(loop, ctx):
            exc = ctx.get("exception")
            if exc is not None:
                name = type(exc).__name__
                msg  = str(exc).lower()
                # Suppress orphaned futures from browser close / abort
                if name in ("TargetClosedError", "ConnectionClosedError",
                            "CancelledError"):
                    return
                # Playwright base Error with net:: abort codes that appear when
                # the browser is closed mid-navigation after Stop
                if name == "Error" and any(code in msg for code in (
                        "err_aborted", "err_empty_response",
                        "err_connection_reset", "err_connection_refused",
                        "target page, context or browser has been closed")):
                    return
            loop.default_exception_handler(ctx)

        loop.set_exception_handler(_exc_handler)

        try:
            self._batch_task = loop.create_task(
                self._execute_batch(targets, year_specs, root_dir, selected_docs))
            loop.run_until_complete(self._batch_task)
        except asyncio.CancelledError:
            self.log("[System] Batch cancelled.")
        except Exception as e:
            self.log(f"[System Error] Batch crashed: {e}")
        finally:
            self._batch_task = None
            self._batch_loop = None
            loop.close()
            self.is_running = False
            self._last_selected_docs = selected_docs
            if self._progress_dialog:
                self._progress_dialog.batch_finished(aborted=self._batch_aborted)
            self._batch_done_signal.emit()

    def _on_batch_done(self):
        import datetime
        self.btn_run.setText("  Downloads")
        self._lock_ui(False)
        self.log("[System] Engine Idle.")
        # Refresh grid so Last Download Status / Last Saved Location columns update
        QTimer.singleShot(200, self.refresh_grid)
        selected_docs = self._last_selected_docs
        run_label = " + ".join(DOC_TYPE_LABELS.get(d, d) for d in sorted(selected_docs)) or "Batch"

        # F-35: restore from tray if we were hidden there, and show tray balloon
        if hasattr(self, "_tray") and self._tray.isVisible():
            notify_windows("AayDocCapio — Download Complete",
                           f"{run_label} batch finished. Click the tray icon to restore.")
            if not self._challan_running:
                self._tray_stop_act.setEnabled(False)
                self._tray_send_act.setVisible(False)
            self._tray.setToolTip("AayDocCapio — Batch complete")
            # Auto-restore after balloon (small delay so user sees the notification)
        # F-35: Windows native toast — only when NOT using tray (tray has its own balloon)
        if not self._batch_aborted and not (hasattr(self, "_tray") and self._tray.isVisible()):
            notify_windows("AayDocCapio — Download Complete",
                           f"{run_label} batch run finished. Click to open the app.")

        if self._batch_aborted:
            self._ais_results = {}
            return

        if "request_ais" in selected_docs:
            results = self._ais_results
            n_instant = sum(1 for v in results.values() if v == "instant")
            n_queued  = sum(1 for v in results.values() if v == "queued")
            n_failed  = sum(1 for v in results.values() if v == "failed")

            self._ais_requested_time = datetime.datetime.now()
            t = self._ais_requested_time.strftime("%I:%M %p")

            # Build status line text — only mention what actually happened
            parts = []
            if n_instant:
                parts.append(f"{n_instant} downloaded instantly")
            if n_queued:
                parts.append(f"{n_queued} queued on ITD servers")
            if n_failed:
                parts.append(f"{n_failed} failed")
            status_summary = " · ".join(parts) if parts else "no results"

            if n_queued:
                self.ais_status_lbl.setText(
                    f"⏳  AIS at {t}: {status_summary} — "
                    f"wait ~5 min then click ▶ Run → Download Previously Requested AIS for the {n_queued} queued client(s).")
                self.ais_status_bar.setVisible(True)
            else:
                # All instant or failed — no need to show the waiting reminder
                self.ais_status_bar.setVisible(False)

            # Build dialog text based on actual breakdown
            lines = ["<b>AIS Request Results:</b><br>"]
            if n_instant:
                lines.append(
                    f"✅ <b>{n_instant} client(s)</b> — AIS file was small, "
                    f"downloaded instantly. No further action needed for these.")
            if n_queued:
                lines.append(
                    f"⏳ <b>{n_queued} client(s)</b> — AIS file is large, "
                    f"generation request queued on ITD servers.<br>"
                    f"&nbsp;&nbsp;Wait <b>~5 minutes</b>, then select these clients "
                    f"and click <b>▶ Run → ⬇ Download Previously Requested AIS</b>.")
            if n_failed:
                # Show the distinct error reasons so the user knows WHY.
                reasons = sorted(set(self._last_errors.values()))
                reason_html = ""
                if reasons:
                    reason_html = "<br>" + "<br>".join(
                        f"&nbsp;&nbsp;• {r}" for r in reasons[:5])
                lines.append(
                    f"❌ <b>{n_failed} client(s)</b> — Request failed.{reason_html}")

            msg = QMessageBox(self)
            msg.setWindowTitle("AIS Request Complete")
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setText("<br><br>".join(lines))
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.exec()

        if "ais_tis" in selected_docs:
            # Hide the status line once download is done
            self.ais_status_bar.setVisible(False)
            self._ais_requested_time = None

        if "26as" in selected_docs and not self._batch_aborted:
            pass  # conversion now happens inline per-client in _execute_batch

    def _convert_26as_manual(self):
        from automation.as26_converter import convert_26as_txt
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select 26AS TXT file(s)",
            self.vault.get_setting("download_root_dir", ""),
            "Text Files (*.txt);;All Files (*)")
        if not paths:
            return
        ok, fail = 0, 0
        locked_warnings = []  # collect "saved as alternate" warnings for the dialog

        def _log_capturing(msg):
            self.log(msg)
            if msg.startswith("[Warning]") and "was open in Excel" in msg:
                locked_warnings.append(msg[len("[Warning] "):])

        for p in paths:
            try:
                xlsx, html = convert_26as_txt(p, log_callback=_log_capturing)
                self.log(f"[Victory] Converted: {os.path.basename(xlsx)} + {os.path.basename(html)}")
                ok += 1
            except Exception as e:
                self.log(f"[Error] Convert failed for {os.path.basename(p)}: {e}")
                fail += 1
        msg = f"Converted {ok} file(s) — Excel + HTML saved alongside each TXT."
        if locked_warnings:
            msg += "\n\nNote — the following file(s) were open in Excel and saved under an alternate name:\n"
            msg += "\n".join(f"  • {w}" for w in locked_warnings)
        if fail:
            msg += f"\n\n{fail} file(s) failed — check the log for details."
        QMessageBox.information(self, "Convert Complete", msg)

    def _convert_ais_json_manual(self):
        from PyQt6.QtWidgets import (
            QFileDialog, QMessageBox, QDialog, QVBoxLayout,
            QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton, QFrame,
        )
        from ui.helpers import _lbl, _btn

        # ── Step 1: pick JSON file ────────────────────────────────────────
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select AIS JSON file(s)",
            self.vault.get_setting("download_root_dir", ""),
            "AIS JSON Files (*.json);;All Files (*)")
        if not paths:
            return

        # ── Step 2: credentials dialog ────────────────────────────────────
        clients = self.vault.get_all_assessees()

        dlg = QDialog(self)
        dlg.setWindowTitle("AIS JSON — Credentials")
        dlg.setFixedWidth(420)
        dlg.setModal(True)
        t = _t()
        dlg.setStyleSheet(
            f"QDialog{{background:{t.bg_window};}}"
            f"QLabel{{color:{t.text_primary};background:transparent;}}"
            f"QLineEdit{{border:1px solid {t.border};border-radius:6px;"
            f"padding:5px 10px;font-size:12px;background:{t.bg_input};"
            f"color:{t.text_primary};}}"
            f"QComboBox{{border:1px solid {t.border};border-radius:6px;"
            f"padding:4px 10px;font-size:12px;background:{t.bg_input};"
            f"color:{t.text_primary};}}"
            f"QComboBox::drop-down{{border:none;width:20px;}}"
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        lay.addWidget(_lbl("Enter PAN and Date of Birth to decrypt the AIS JSON.", 11,
                           color=t.text_muted))

        # Vault shortcut — sorted, searchable combo
        sorted_clients = sorted(clients, key=lambda x: x.get("name", "").lower()) if clients else []

        if sorted_clients:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QCompleter

            combo_lbl = QLabel("Select from vault (optional):")
            combo_lbl.setStyleSheet("font-size:11px;font-weight:600;")
            lay.addWidget(combo_lbl)

            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setMaxVisibleItems(12)
            combo.setFixedHeight(34)
            combo.addItem("", None)
            for c in sorted_clients:
                combo.addItem(f"{c['name']}  ({c['pan']})", c)

            completer = QCompleter([f"{c['name']}  ({c['pan']})" for c in sorted_clients])
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.popup().setStyleSheet(
                f"QListView{{background:{t.bg_input};color:{t.text_primary};"
                f"border:1px solid {t.border};border-radius:4px;"
                f"padding:2px;font-size:12px;}}"
                f"QListView::item{{padding:4px 8px;}}"
                f"QListView::item:hover{{background:{t.accent_light};color:{t.text_primary};}}"
                f"QListView::item:selected{{background:{t.accent};color:{t.accent_text};}}"
            )
            combo.setCompleter(completer)
            combo.lineEdit().setPlaceholderText("Type name or PAN to search…")
            combo.setCurrentIndex(-1)
            lay.addWidget(combo)

            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"background:{t.border};border:none;max-height:1px;")
            lay.addWidget(sep)

        pan_lbl = QLabel("PAN:")
        pan_lbl.setStyleSheet("font-size:11px;font-weight:600;")
        lay.addWidget(pan_lbl)
        pan_edit = QLineEdit()
        pan_edit.setPlaceholderText("e.g. AFCPB9287R")
        pan_edit.setFixedHeight(34)
        lay.addWidget(pan_edit)

        dob_lbl = QLabel("Date of Birth:")
        dob_lbl.setStyleSheet("font-size:11px;font-weight:600;")
        lay.addWidget(dob_lbl)
        dob_edit = QLineEdit()
        dob_edit.setPlaceholderText("DD-MM-YYYY  (e.g. 09-07-1979)")
        dob_edit.setFixedHeight(34)
        lay.addWidget(dob_edit)

        if sorted_clients:
            def _on_client_selected(idx):
                c = combo.itemData(idx)
                if c:
                    pan_edit.setText(c.get("pan", ""))
                    dob_edit.setText(c.get("dob", ""))
                else:
                    pan_edit.clear(); dob_edit.clear()
            combo.currentIndexChanged.connect(_on_client_selected)

            def _on_text_activated(text):
                idx = combo.findText(text)
                if idx >= 0:
                    _on_client_selected(idx)
            combo.lineEdit().editingFinished.connect(
                lambda: _on_text_activated(combo.currentText())
            )

        btn_row = QHBoxLayout()
        ok_btn = _btn("Convert", "primary", height=36)
        cancel_btn = _btn("Cancel", "secondary", height=36)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pan_val = pan_edit.text().strip().upper()
        dob_val = dob_edit.text().strip()
        if not pan_val or not dob_val:
            QMessageBox.warning(self, "Missing Credentials",
                                "Please enter both PAN and Date of Birth.")
            return

        # ── Step 3: convert each file ─────────────────────────────────────
        from automation.ais_converter import convert_ais_json
        ok_files, fail_files = [], []

        for p in paths:
            try:
                xlsx = convert_ais_json(p, log_callback=self.log,
                                        pan=pan_val, dob=dob_val)
                self.log(f"[Victory] AIS converted: {os.path.basename(xlsx)}")
                ok_files.append(os.path.basename(xlsx))
            except ValueError:
                self.log(f"[Error] AIS decrypt failed for {os.path.basename(p)} "
                         f"— check PAN / DOB.")
                fail_files.append(os.path.basename(p))
            except Exception as e:
                self.log(f"[Error] AIS convert failed for {os.path.basename(p)}: {e}")
                fail_files.append(os.path.basename(p))

        msg = f"Converted {len(ok_files)} AIS JSON file(s) to Excel."
        if ok_files:
            msg += "\n\nSaved alongside source JSON:\n"
            msg += "\n".join(f"  • {f}" for f in ok_files)
        if fail_files:
            msg += f"\n\n{len(fail_files)} file(s) failed — check PAN / DOB and the log."
            msg += "\n" + "\n".join(f"  • {f}" for f in fail_files)
        QMessageBox.information(self, "AIS Convert Complete", msg)

    def _auto_convert_26as(self, set_status=None):
        from automation.as26_converter import convert_26as_txt
        items = getattr(self, "_batch_26as_txts", [])
        if not items:
            return
        converted = 0
        for pan, txt in items:
            if not os.path.exists(txt):
                continue
            if set_status:
                set_status(pan, "⏳ Converting to Excel...")
            try:
                convert_26as_txt(txt, log_callback=self.log)
                converted += 1
                if set_status:
                    set_status(pan, "✅ 26AS + Excel + HTML")
            except Exception as e:
                self.log(f"[Warning] Auto-convert failed for {os.path.basename(txt)}: {e}")
                if set_status:
                    set_status(pan, "⚠ Excel convert failed")
        if converted:
            self.log(f"[Victory] Auto-converted {converted} 26AS TXT file(s) to Excel + HTML.")

    async def _execute_batch(self, targets, year_specs, root_dir, selected_docs):
        selected_docs = set(selected_docs)
        years_desc = ", ".join(s["ay_label"] for s in year_specs)
        self.log(f"[System] Batch: {len(targets)} client(s) | Year(s): {years_desc} | Docs: {', '.join(sorted(selected_docs))}")
        self._ais_results = {}
        self._last_errors = {}
        self._batch_26as_txts = []

        _client_out      = {}   # (pan, ay_label) → out path, populated below
        _last_terminal   = {}   # (pan, ay_label) → last terminal status text this run
        _retried:  dict  = {}   # pan → True if already retried once
        _retry_queue     = []   # targets queued for one retry after main loop

        def _is_transient(err: str) -> bool:
            e = err.lower()
            return any(x in e for x in [
                "timeout", "assessmentyear", "could not find asses",
                "ais failed", "tis failed", "navigation", "net::",
            ])

        def set_status(pan, ay_label, text, doc_type=None):
            """Update progress dialog and persist terminal status to vault,
            keyed per (pan, ay_label, doc_type) so one document type's status
            can't overwrite another's, and one year can't overwrite another's,
            within a multi-select multi-year batch."""
            if self._progress_dialog:
                self._progress_dialog.set_status(pan, ay_label, text)
            terminal = ("✅", "❌", "🕐", "⏹", "⬜", "⚠")
            if ay_label and any(text.startswith(p) for p in terminal):
                key = (pan, ay_label)
                _last_terminal[key] = text
                if doc_type:
                    try:
                        self.vault.record_download(
                            pan, ay_label, doc_type, text, _client_out.get(key, ""))
                    except Exception:
                        pass

        try:
            interactive = not self.chk_headless.isChecked()
            context = await browser_manager.get_context(
                log_callback=self.log, interactive=interactive)
        except Exception as e:
            self.log(f"[System Error] Browser init failed: {e}"); return

        # Warm up the ITD portal before the first client — loads the Angular bundle,
        # CDN assets and sets session cookies so the first client's dashboard renders
        # at the same speed as subsequent clients.
        try:
            self.log("[System] Warming up ITD portal...")
            _warmup_page = await context.new_page()
            await _warmup_page.goto(
                "https://eportal.incometax.gov.in/iec/foservices/#/login",
                wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            await _warmup_page.close()
            self.log("[System] Portal warm-up done.")
        except Exception as _wu_err:
            self.log(f"[System] Portal warm-up skipped: {_wu_err}")

        try:
            for i, target in enumerate(targets):
                if not self.is_running:
                    self.log("[System] Aborted."); break

                pan  = target.get("pan", "")
                name = target.get("name", "")
                dob  = target.get("dob", "")
                self._skip_current = False   # reset skip flag for each new client
                if self._progress_dialog:
                    self._progress_dialog.client_started()
                self.log("──────────────────────────────────────────────────")
                self.log(f"[{i+1}/{len(targets)}] {name}")

                name_safe = "".join(c if c.isalnum() or c in " _-" else "" for c in name)

                def _out_for_year(year_type, value, _root=root_dir, _pan=pan, _name_safe=name_safe):
                    return os.path.join(_root, f"{_pan}-{_name_safe}", f"{year_type}_{value.replace('-','_')}")

                # Tell the progress dialog each year's save folder immediately
                for _spec in year_specs:
                    _out = _out_for_year(_spec["year_type"], _spec["value"])
                    _client_out[(pan, _spec["ay_label"])] = _out
                    if self._progress_dialog:
                        self._progress_dialog.set_client_path(pan, _spec["ay_label"], _out)

                page = None
                try:
                    # ── Login ────────────────────────────────────────────────────
                    for _spec in year_specs:
                        set_status(pan, _spec["ay_label"], "⏳ Logging in to ITD...")
                    page = await login_itd(pan, target.get("password"), self.log, context,
                                           is_running=lambda: self.is_running and not self._skip_current)
                    for _spec in year_specs:
                        set_status(pan, _spec["ay_label"], "⏳ Logged in to ITD")

                    # ── Skip check ───────────────────────────────────────────────
                    if self._skip_current:
                        # Update progress dialog only — do NOT persist to vault or log history
                        if self._progress_dialog:
                            for _spec in year_specs:
                                self._progress_dialog.set_status(pan, _spec["ay_label"], "⬜ Skipped")
                                self._progress_dialog.client_finished(pan, _spec["ay_label"])
                        self.log(f"[Skip] {pan[:3]}XXXXXXX skipped by user.")
                        if page:
                            try: await logout_itd(page, self.log)
                            except Exception: pass
                        page = None
                        await asyncio.sleep(1)
                        continue

                    # ── Selected document types, one client fully before the next ──
                    # Dispatch order is deterministic (not raw set iteration)
                    # so Filed Returns always runs before 26AS — see
                    # ordered_doc_types() docstring for why. Every doc type
                    # runs once per client and loops all selected years
                    # internally (F-14: doc-type-by-doc-type across years).
                    for doc_type in ordered_doc_types(selected_docs):
                        if not self.is_running:
                            break
                        handler = HANDLERS.get(doc_type)
                        if handler is None:
                            continue
                        doc_set_status = (lambda p, yl, t, _dt=doc_type: set_status(p, yl, t, _dt))
                        result = await handler(
                            page, pan, dob, year_specs, _out_for_year, self.log, doc_set_status,
                            filing_scope=getattr(self, "_batch_filing_scope", "all"),
                            is_running=(lambda: self.is_running),
                        ) or {}

                        if doc_type == "26as":
                            for _txt_path in (result.get("txt_paths") or {}).values():
                                self._batch_26as_txts.append((pan, _txt_path))
                        elif doc_type == "request_ais":
                            for _ay_lbl, _ais_status in (result.get("ais_statuses") or {}).items():
                                _key = (pan, _ay_lbl)
                                if _ais_status == "downloaded":
                                    self._ais_results[_key] = "instant"
                                elif _ais_status == "requested":
                                    self._ais_results[_key] = "queued"
                                elif _ais_status == "skipped":
                                    self._ais_results[_key] = "skipped"
                                else:
                                    self._ais_results[_key] = "failed"

                    if self.is_running:
                        await logout_itd(page, self.log)
                        page = None

                    pan_masked = pan[:3] + "XXXXXXX" if pan and len(pan) >= 3 else "UNKNOWN"
                    self.log(f"[Victory] {pan_masked} done.")

                except Exception as e:
                    pan_masked = pan[:3] + "XXXXXXX" if pan and len(pan) >= 3 else "UNKNOWN"
                    self.log(f"[Error] {pan_masked}: {e}")
                    if _is_transient(str(e)) and not _retried.get(pan) and self.is_running:
                        # Transient error — logout cleanly and queue for one retry
                        _retried[pan] = True
                        self.log(f"[Retry] Transient error detected — will retry {pan_masked} after batch.")
                        for _spec in year_specs:
                            set_status(pan, _spec["ay_label"], f"🕐 Queued for retry — {_friendly_error(str(e))}")
                        if page:
                            try: await logout_itd(page, self.log)
                            except Exception: pass
                        _retry_queue.append(target)
                    else:
                        # Permanent failure or already retried
                        friendly = _friendly_error(str(e))
                        for _spec in year_specs:
                            _key = (pan, _spec["ay_label"])
                            self._ais_results[_key] = "failed"
                            self._last_errors[_key] = str(e)
                            if self._batch_aborted or friendly == "Stopped by user":
                                # Batch was stopped — do NOT overwrite last saved status in vault
                                if self._progress_dialog:
                                    self._progress_dialog.set_status(pan, _spec["ay_label"], f"⏹ Stopped")
                            else:
                                set_status(pan, _spec["ay_label"], f"❌ Failed — {friendly}")
                        if page:
                            try: await logout_itd(page, self.log)
                            except Exception: pass

                # Append the final status (whatever the main grid now shows) to log history
                for _spec in year_specs:
                    _key = (pan, _spec["ay_label"])
                    if _key in _last_terminal:
                        try:
                            self.log_store.record(pan, _spec["ay_label"], _last_terminal.pop(_key))
                        except Exception:
                            pass

                # This client's turn in the main loop is over — ALL its selected
                # doc types have now run (success, failure, or queued for retry)
                # for EVERY selected year. This is the only correct signal that
                # the progress dialog should count each (client, year) row as
                # done; a multi-select batch produces several terminal-looking
                # status updates per client along the way (one per doc type per
                # year), so those can no longer be used to infer "done".
                if self._progress_dialog:
                    for _spec in year_specs:
                        self._progress_dialog.client_finished(pan, _spec["ay_label"])

                await asyncio.sleep(3)

            # ── Retry pass — one attempt per transient-failed client ──────────
            if _retry_queue and self.is_running:
                self.log(f"[Retry] Retrying {len(_retry_queue)} client(s) that had transient errors...")
                for target in _retry_queue:
                    if not self.is_running:
                        break
                    pan  = target.get("pan", "")
                    name = target.get("name", "")
                    dob  = target.get("dob", "")
                    pan_masked = pan[:3] + "XXXXXXX" if pan and len(pan) >= 3 else "UNKNOWN"
                    self.log("──────────────────────────────────────────────────")
                    self.log(f"[Retry] {name}")
                    name_safe = "".join(c if c.isalnum() or c in " _-" else "" for c in name)

                    def _out_for_year(year_type, value, _root=root_dir, _pan=pan, _name_safe=name_safe):
                        return os.path.join(_root, f"{_pan}-{_name_safe}", f"{year_type}_{value.replace('-','_')}")

                    page = None
                    try:
                        for _spec in year_specs:
                            set_status(pan, _spec["ay_label"], "⏳ Logging in to ITD (retry)...")
                        page = await login_itd(pan, target.get("password"), self.log, context,
                                               is_running=lambda: self.is_running)

                        for doc_type in ordered_doc_types(selected_docs):
                            if not self.is_running:
                                break
                            handler = HANDLERS.get(doc_type)
                            if handler is None:
                                continue
                            doc_set_status = (lambda p, yl, t, _dt=doc_type: set_status(p, yl, t, _dt))
                            result = await handler(
                                page, pan, dob, year_specs, _out_for_year, self.log, doc_set_status,
                                filing_scope=getattr(self, "_batch_filing_scope", "all"),
                                is_running=(lambda: self.is_running),
                            ) or {}
                            if doc_type == "26as":
                                for _txt_path in (result.get("txt_paths") or {}).values():
                                    self._batch_26as_txts.append((pan, _txt_path))
                            elif doc_type == "request_ais":
                                for _ay_lbl, _ais_status in (result.get("ais_statuses") or {}).items():
                                    self._ais_results[(pan, _ay_lbl)] = (
                                        "instant" if _ais_status == "downloaded"
                                        else "queued" if _ais_status == "requested"
                                        else "skipped" if _ais_status == "skipped"
                                        else "failed")

                        if self.is_running:
                            await logout_itd(page, self.log)
                        self.log(f"[Retry] {pan_masked} done.")
                    except Exception as e:
                        self.log(f"[Retry Error] {pan_masked}: {e}")
                        for _spec in year_specs:
                            _key = (pan, _spec["ay_label"])
                            self._ais_results[_key] = "failed"
                            self._last_errors[_key] = str(e)
                            set_status(pan, _spec["ay_label"], f"❌ Failed — {_friendly_error(str(e))}")
                        if page:
                            try: await logout_itd(page, self.log)
                            except Exception: pass

                    for _spec in year_specs:
                        _key = (pan, _spec["ay_label"])
                        if _key in _last_terminal:
                            try:
                                self.log_store.record(pan, _spec["ay_label"], _last_terminal.pop(_key))
                            except Exception:
                                pass
                    await asyncio.sleep(3)

        finally:
            await browser_manager.close()
        self.log("[System] Batch finished.")

    async def _ensure_dashboard(self, page):
        """Navigate back to ITD dashboard if not already there."""
        try:
            if "dashboard" not in page.url.lower():
                await page.goto(
                    "https://eportal.incometax.gov.in/iec/fo/dashboard",
                    wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
        except Exception:
            pass


class _SplashScreen(QWidget):
    """Startup splash — the wordmark logo with a pulsing gold glow behind
    it, an animated "Starting AayDocCapio..." dot cycle, and fade in/out,
    shown while AayDocCapioApp() (imports Playwright, builds the full main
    window) is still constructing. Deliberately a plain QWidget rather than
    QSplashScreen — QSplashScreen has no easy way to layer a QGraphicsEffect
    or animate its own opacity, both needed here."""

    def __init__(self, pixmap: QPixmap):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 28)
        outer.setSpacing(12)

        logo_label = QLabel()
        logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Soft gold glow (brand accent) behind the logo — pulses via the
        # blurRadius animation below rather than sitting static.
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setColor(QColor("#E8B84B"))
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(20)
        logo_label.setGraphicsEffect(self._glow)
        outer.addWidget(logo_label)

        self._status_label = QLabel("Starting AayDocCapio")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(
            "color:#5B6472;font-size:13px;background:transparent;")
        outer.addWidget(self._status_label)

        self.setFixedSize(pixmap.width() + 80, pixmap.height() + 96)
        self.setWindowOpacity(0.0)

        self._dot_count = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start(450)

        # Pulse: one QPropertyAnimation loop, low -> high -> low blur, so
        # it reads as breathing rather than a static halo.
        self._glow_anim = QPropertyAnimation(self._glow, b"blurRadius", self)
        self._glow_anim.setDuration(1400)
        self._glow_anim.setKeyValueAt(0.0, 18)
        self._glow_anim.setKeyValueAt(0.5, 55)
        self._glow_anim.setKeyValueAt(1.0, 18)
        self._glow_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._glow_anim.setLoopCount(-1)
        self._glow_anim.start()

        self._fade_anim = None  # kept alive on self so it isn't GC'd mid-animation

    def _tick_dots(self):
        self._dot_count = (self._dot_count + 1) % 4
        self._status_label.setText("Starting AayDocCapio" + "." * self._dot_count)

    def fade_in(self):
        self.show()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(350)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._fade_anim = anim

    def finish(self, window):
        """Fades out and closes — named/shaped like QSplashScreen.finish()
        for a familiar call site, but doesn't block: the fade plays out
        during app.exec()'s own event loop instead of freezing startup."""
        self._dot_timer.stop()
        self._glow_anim.stop()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(300)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self.close)
        anim.start()
        self._fade_anim = anim


def _fatal(msg: str):
    """Show a visible error dialog even before QApplication exists, then exit."""
    try:
        # Try Qt dialog first (works if Qt DLLs loaded successfully)
        _a = QApplication.instance() or QApplication(sys.argv)
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox()
        box.setWindowTitle("AayDocCapio — Startup Error")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("AayDocCapio could not start.")
        box.setDetailedText(msg)
        box.exec()
    except Exception:
        # Qt itself failed — fall back to Windows MessageBox via ctypes
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"AayDocCapio could not start.\n\n{msg}\n\n"
                "If this persists, install the Microsoft Visual C++ Redistributable:\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe",
                "AayDocCapio — Startup Error",
                0x10  # MB_ICONERROR
            )
        except Exception:
            pass
    sys.exit(1)


if __name__ == "__main__":
    # Suppress harmless "unclosed transport" ResourceWarning noise on Windows
    # caused by asyncio ProactorEventLoop GC during shutdown.
    if sys.platform == "win32":
        import warnings
        warnings.filterwarnings("ignore", category=ResourceWarning,
                                message=".*unclosed transport.*")

    # Called by Inno Setup [Run] step to pre-install Chromium silently
    if "--install-browsers" in sys.argv:
        from automation.browser import _install_chromium
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_install_chromium())
        except Exception as e:
            print(f"Browser install failed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            loop.close()
        sys.exit(0)

    # Write a startup trace log using only builtins — before any Qt call
    _diag_path = os.path.join(_app_dir(), "startup_diag.log")
    def _diag(msg):
        try:
            os.makedirs(os.path.dirname(_diag_path), exist_ok=True)
            with open(_diag_path, "a", encoding="utf-8") as _f:
                _f.write(msg + "\n")
        except Exception:
            pass

    _diag(f"\n=== Startup {datetime.datetime.now()} ===")
    _diag(f"bundled_dir : {_bundled_dir()}")
    _diag(f"app_dir     : {_app_dir()}")
    _diag(f"sys.argv    : {sys.argv}")
    _diag(f"platform    : {sys.platform}")

    try:
        _diag("Step 1: QApplication()")
        # Suppress harmless font-db OpenType warnings from QFontComboBox scanning
        from PyQt6.QtCore import qInstallMessageHandler
        def _qt_msg_filter(msg_type, context, msg):
            if "OpenType support missing" in msg or "qt.text.font.db" in msg:
                return
            import sys as _sys
            _sys.stderr.write(msg + "\n")
        qInstallMessageHandler(_qt_msg_filter)
        app = QApplication(sys.argv)
        _diag("Step 1b: splash screen")
        # Shown immediately, before the (noticeably slower) font/theme/
        # AayDocCapioApp() construction steps below — those import
        # Playwright and build the full main window, which can take a
        # visible moment with nothing else on screen otherwise.
        splash = None
        try:
            _splash_logo_path = os.path.join(_bundled_dir(), "resources", "AayDoc_FullLogo.png")
            if os.path.exists(_splash_logo_path):
                _splash_pix = QPixmap(_splash_logo_path).scaledToWidth(
                    560, Qt.TransformationMode.SmoothTransformation)
                splash = _SplashScreen(_splash_pix)
                splash.fade_in()
                app.processEvents()
        except Exception as _splash_err:
            _diag(f"Splash screen skipped: {_splash_err}")
            splash = None
        _diag("Step 2: setApplicationName")
        app.setApplicationName("AayDocCapio")
        app.setDesktopFileName("aay-doc-capio")
        app.setStyle("Fusion")
        # Force light palette so macOS dark mode doesn't corrupt unstyled
        # native widgets (dialogs, menus, headers). Will be overridden once
        # the theme system initialises inside AayDocCapioApp.
        try:
            app.styleHints().setColorScheme(Qt.ColorScheme.Light)
        except AttributeError:
            pass  # Qt < 6.8
        _diag("Step 3: font loading")
        from PyQt6.QtGui import QFontDatabase
        _fonts_dir = os.path.join(_bundled_dir(), "resources", "fonts")
        if os.path.isdir(_fonts_dir):
            for _ttf in os.listdir(_fonts_dir):
                if _ttf.endswith(".ttf"):
                    QFontDatabase.addApplicationFont(os.path.join(_fonts_dir, _ttf))
        _diag("Step 4: setStyleSheet")
        app.setStyleSheet(build_stylesheet(get_theme("light")))
        _diag("Step 5: AayDocCapioApp()")
        window = AayDocCapioApp()
        _diag("Step 6: setWindowIcon")
        _app_icon_path = os.path.join(_bundled_dir(), "resources", "app_icon.png")
        if os.path.exists(_app_icon_path):
            _icon = QIcon(_app_icon_path)
            app.setWindowIcon(_icon)
            window.setWindowIcon(_icon)
        _diag("Step 7: window.show()")
        window.show()
        if splash is not None:
            # finish() closes the splash the moment `window` is shown/
            # active rather than needing a manual timer to guess when
            # startup is "done".
            splash.finish(window)
        _diag("Step 8: app.exec() — entering event loop")
        sys.exit(app.exec())
    except Exception as _startup_err:
        import traceback
        _tb = traceback.format_exc()
        _diag(f"CRASH:\n{_tb}")
        _fatal(_tb)
