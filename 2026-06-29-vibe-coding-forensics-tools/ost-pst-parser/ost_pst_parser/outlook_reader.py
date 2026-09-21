#!/usr/bin/env python3

import argparse
import os
import sqlite3
import sys

from .ui_helpers import (
    apply_qt_app_icon,
    log_error,
    log_event,
    normalize_native_path,
    pause_for_console,
    suppress_qt_font_warnings,
)


SQLITE_MAGIC = b"SQLite format 3\x00"
PFF_EXTENSIONS = {".ost", ".pst", ".pff"}
SQLITE_EXTENSIONS = {".sqlite", ".sqlite3", ".db"}


def load_mac_reader_class():
    """Load the Mac Outlook SQLite reader class."""
    from .mac_outlook_reader import MacOutlookReader

    return MacOutlookReader


def file_magic(path, size=16):
    """Return the first bytes of a file for type detection."""
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def has_sqlite_outlook_schema(path):
    """Return whether a SQLite database has the expected Outlook tables."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table' and name in ('Mail', 'Folders')"
                )
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return {"Mail", "Folders"}.issubset(tables)


def detect_reader_type(path):
    """Detect whether a mailbox path should use the SQLite or PFF reader."""
    ext = os.path.splitext(path)[1].lower()
    magic = file_magic(path)

    if magic == SQLITE_MAGIC:
        return "sqlite" if has_sqlite_outlook_schema(path) else "unknown-sqlite"
    if ext in SQLITE_EXTENSIONS and has_sqlite_outlook_schema(path):
        return "sqlite"
    if ext in PFF_EXTENSIONS:
        return "pff"
    if has_sqlite_outlook_schema(path):
        return "sqlite"
    return "unknown"


def main(argv=None):
    """Run the command-line reader detection and launch the selected UI."""
    try:
        from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            message = (
                "Missing Qt dependency: PySide6\n\n"
                "Install the required GUI package with:\n"
                "  python -m pip install PySide6\n\n"
                "For PST/OST/PFF support as well, use:\n"
                "  python -m pip install PySide6 libpff-python"
            )
            log_error("Missing Qt dependency", message)
            pause_for_console()
            return 1
        raise
    from .ost_viewer import OSTViewer

    parser = argparse.ArgumentParser(description="Open Outlook SQLite, PST, OST, or PFF evidence.")
    parser.add_argument("path", nargs="?", help="Mailbox path to open.")
    parser.add_argument(
        "--light-load",
        "--low-io",
        action="store_true",
        dest="light_load_mode",
        help="For PST/OST/PFF files, skip global timeline and contact-cache scans.",
    )
    args = parser.parse_args(list(argv or sys.argv[1:]))
    path = args.path

    suppress_qt_font_warnings()
    app = QApplication([sys.argv[0]])
    apply_qt_app_icon(app)
    if not path:
        path, _ = QFileDialog.getOpenFileName(
            None,
            "Open Outlook mailbox",
            "",
            "Outlook evidence (*.sqlite *.sqlite3 *.db *.ost *.pst *.pff);;All files (*)",
        )
        if not path:
            return 0

    path = normalize_native_path(path)
    log_event(f"Selected input file: {path}")
    reader_type = detect_reader_type(path)
    log_event(f"Detected reader type: {reader_type}")
    if reader_type == "sqlite":
        window_cls = load_mac_reader_class()
    elif reader_type == "pff":
        window_cls = OSTViewer
    elif reader_type == "unknown-sqlite":
        message = "This SQLite file does not contain Mail and Folders tables."
        log_error("Unsupported SQLite database", message)
        QMessageBox.critical(None, "Unsupported SQLite database", message)
        return 2
    else:
        message = "Unable to identify this file as an Outlook SQLite, PST, or OST file."
        log_error("Unsupported file", message)
        QMessageBox.critical(None, "Unsupported file", message)
        return 2

    log_event(f"Launching UI for {reader_type} reader")
    if reader_type == "pff":
        window = window_cls(initial_file=path, light_load_mode=args.light_load_mode)
    else:
        window = window_cls(initial_file=path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
