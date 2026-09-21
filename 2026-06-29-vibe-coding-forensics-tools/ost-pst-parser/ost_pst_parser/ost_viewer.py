import atexit
import argparse
import base64
import html as html_lib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from .ui_helpers import (
    apply_qt_app_icon,
    log_error,
    log_event,
    normalize_native_path,
    pause_for_console,
    suppress_qt_font_warnings,
)

try:
    from PySide6.QtCore import (
        Qt, QAbstractTableModel, QModelIndex,
        QThread, Signal, QObject, QTimer
    )
    from PySide6.QtGui import QColor, QBrush, QFontDatabase, QSyntaxHighlighter, QTextCharFormat

    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QTreeWidget, QTreeWidgetItem,
        QTableView, QTextBrowser, QPlainTextEdit, QSplitter,
        QFileDialog, QWidget, QVBoxLayout,
        QLineEdit, QLabel, QProgressBar, QMessageBox,
        QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
        QPushButton, QHeaderView, QAbstractItemView, QHBoxLayout,
        QStackedWidget
    )
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        message = (
            "Missing Qt dependency: PySide6\n\n"
            "Install the required GUI package with:\n"
            "  python -m pip install PySide6\n\n"
            "For the full PFF/PST/OST reader environment, use:\n"
            "  python -m pip install PySide6 libpff-python"
        )
        log_error("Missing Qt dependency", message)
        pause_for_console()
        raise SystemExit(1) from exc
    raise

from .email_domains import contains_external_email, contains_personal_email
from .agenda_view import AgendaItem, AgendaModel, AgendaTwoWeekView, agenda_datetime_text
from .table_colors import (
    attachment_brush,
    dark_text_brush,
    external_address_brush,
    heat_brushes,
    personal_address_brush,
)
from .pff_access import (
    attachment_bytes,
    attachment_count,
    exposed_attachment_count,
    exposed_attachment_row_count,
    attachment_indicator_details,
    build_recipient_cache,
    contact_display,
    create_pff_reader,
    debug_exception,
    email_classification_info,
    email_description_from_snapshot,
    folder_message_count,
    folder_name,
    folder_subfolder_at,
    folder_subfolder_count,
    format_bytes,
    get_value,
    message_class,
    message_date,
    message_date_sort_key,
    message_size,
    message_snapshot,
    record_value,
    resolve_bcc,
    resolve_body,
    resolve_cc,
    resolve_sender,
    resolve_to,
    s,
    short_display,
)
from .rendering import (
    append_html_fragment,
    attachment_images_html,
    image_attachment_mime,
    plain_body_html,
    renderable_html,
    rtf_to_html,
    rtf_to_text,
    strip_html,
)
from .timeline_view import TimelineBarChart, folder_color, normalize_message_datetime


TEMP_ATTACHMENT_DIRS = set()
AUTO_LIGHT_LOAD_SECONDS = 30.0
AUTO_LIGHT_LOAD_OPEN_SECONDS = 10.0


def cleanup_temp_attachment_dirs():
    """Remove temporary attachment directories created by Open."""
    for temp_dir in list(TEMP_ATTACHMENT_DIRS):
        log_event(f"Deleting temporary attachment folder: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        TEMP_ATTACHMENT_DIRS.discard(temp_dir)


atexit.register(cleanup_temp_attachment_dirs)


# =========================================================
# MODEL
# =========================================================
def label_text_is_dark(color):
    """Return label text is dark."""
    return (color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114) < 128


def message_effective_datetime(msg):
    """Return message effective datetime."""
    if isinstance(msg, PffMessageRow):
        if msg.date_sort_kind == "date":
            parsed = normalize_message_datetime(msg.date_sort_value)
            if parsed is not None:
                return parsed
        return normalize_message_datetime(msg.date)
    kind, value = message_date_sort_key(msg)
    if kind == "date":
        parsed = normalize_message_datetime(value)
        if parsed is not None:
            return parsed
    parsed = normalize_message_datetime(message_date(msg))
    return parsed


def pff_row_search_text(row):
    """Return searchable message fields for an indexed PFF row."""
    return " ".join([
        s(row.item_class),
        s(row.type_label),
        s(row.subject),
        s(row.contact),
        s(row.sender),
        s(row.to),
        s(row.cc),
        s(row.bcc),
        s(row.classification),
        s(row.date),
        s(row.folder_name),
    ])


def folder_leaf(folder_path):
    """Return the final folder name from a PST folder path."""
    parts = [part for part in re.split(r"[\\/]+", s(folder_path)) if part]
    return parts[-1] if parts else s(folder_path)


def format_duration(seconds):
    """Return compact duration text."""
    try:
        seconds = max(int(round(seconds)), 0)
    except Exception:
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def normalized_cid_key(value):
    """Return a normalized CID/attachment lookup key."""
    text = html_lib.unescape(s(value)).strip().strip("<>").strip()
    if text.lower().startswith("cid:"):
        text = text[4:]
    return text.strip().lower()


def cid_key_variants(value):
    """Return lookup variants for a CID or attachment filename."""
    key = normalized_cid_key(value)
    if not key:
        return set()
    variants = {key}
    if "@" in key:
        variants.add(key.split("@", 1)[0])
    basename = os.path.basename(key)
    if basename:
        variants.add(basename)
        if "@" in basename:
            variants.add(basename.split("@", 1)[0])
    return {variant for variant in variants if variant}


def attachment_cid_values(info):
    """Return attachment values that can match cid: image references."""
    values = {info.get("name")}
    for entry in info.get("properties", []):
        prop_id = s(entry.get("property_id")).lower()
        name = s(entry.get("name")).lower()
        if prop_id in {"0x3001", "0x3704", "0x3707", "0x3712", "0x3713"} or "filename" in name or "content id" in name or "content location" in name:
            values.add(entry.get("value"))
    return values


def inline_cid_images(html, attachments):
    """Replace matching cid: image sources with base64 data URLs."""
    if not html or "cid:" not in html.lower() or not attachments:
        return html

    cid_map = {}
    for info in attachments:
        data = attachment_bytes(info.get("_object"))
        if not data:
            continue
        mime_type = s(info.get("type")) or image_attachment_mime(info) or mimetypes.guess_type(s(info.get("name")))[0] or "application/octet-stream"
        data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
        for value in attachment_cid_values(info):
            for key in cid_key_variants(value):
                cid_map.setdefault(key, data_url)

    if not cid_map:
        return html

    def replace_src(match):
        quote = match.group(1)
        cid_value = match.group(2)
        for key in cid_key_variants(cid_value):
            data_url = cid_map.get(key)
            if data_url:
                return f"src={quote}{data_url}{quote}"
        return match.group(0)

    return re.sub(
        r"src\s*=\s*(['\"])\s*cid:([^'\"]+)\1",
        replace_src,
        html,
        flags=re.IGNORECASE,
    )


def render_warning_html(message):
    """Return a compact warning block for rendered message HTML."""
    return (
        "<div style='font-family: Arial, Helvetica, sans-serif; font-size: 13px; "
        "background: #fff3cd; color: #3b2f00; border: 1px solid #d6b656; "
        "padding: 8px 10px; margin: 0 0 10px 0;'>"
        f"{html_lib.escape(s(message))}"
        "</div>"
    )


def render_section_html(title, content, warning=None):
    """Return a labeled rendered-message section."""
    warning_html = render_warning_html(warning) if warning else ""
    return (
        "<section style='font-family: Arial, Helvetica, sans-serif; "
        "font-size: 13px; margin: 0 0 18px 0;'>"
        f"<h3 style='font-family: Arial, Helvetica, sans-serif; font-size: 13px; "
        "font-weight: 700; margin: 0 0 8px 0; padding: 0; color: #1f2937;'>"
        f"{html_lib.escape(s(title))}</h3>"
        f"{warning_html}{content or ''}"
        "</section>"
    )


def render_section_break():
    """Return a simple separator between rendered message sections."""
    return "<hr style='border: 0; border-top: 1px solid #999; margin: 16px 0;'>"


def prepend_html_fragment(body, fragment):
    """Prepend an HTML fragment inside a document body when possible."""
    if not fragment:
        return body
    match = re.search(r"<body\b[^>]*>", body, flags=re.IGNORECASE)
    if match:
        return body[:match.end()] + fragment + body[match.end():]
    return fragment + body


def rtf_body_unparsable(rtf_body, rtf_text):
    """Return whether an RTF payload was present but could not be parsed."""
    raw = s(rtf_body).strip()
    if not raw:
        return False
    if not raw.startswith("{\\rtf"):
        return True
    return not bool(s(rtf_text).strip())


def build_indexed_search_text(row, msg):
    """Return mailbox search text for a pre-indexed PFF row."""
    try:
        html, text, rtf = resolve_body(msg)
    except Exception:
        html, text, rtf = "", "", ""
    return "\n".join((
        pff_row_search_text(row),
        strip_html(html),
        s(text),
        rtf_to_text(rtf),
    ))


def pff_agenda_item_from_row(row, msg, folder_label):
    """Return an agenda item for a PFF row when it represents a meeting."""
    item_class = row.item_class
    if not is_pff_agenda_class(item_class):
        return None
    try:
        start = pff_agenda_datetime(msg, AGENDA_START_PROPERTIES) or message_effective_datetime(row)
        end = pff_agenda_datetime(msg, AGENDA_END_PROPERTIES)
        html, text, rtf = resolve_body(msg)
        preview = text or rtf_to_text(rtf) or strip_html(html)
    except Exception:
        start = message_effective_datetime(row)
        end = None
        preview = ""
    return AgendaItem(
        start=start,
        end=end,
        subject=row.subject,
        organizer=row.sender,
        attendees=row.to,
        status=pff_agenda_status(item_class),
        item_type=row.type_label,
        folder=folder_label,
        source=item_class,
        source_ref=row,
        details={
            "Message Class": item_class,
            "Contact": row.contact,
            "From": row.sender,
            "To": row.to,
            "CC": row.cc,
            "BCC": row.bcc,
            "Classification": row.classification,
            "Message Date": row.date,
            "Preview": preview,
            "Attachments": row.attachment_count,
            "Size": format_bytes(row.size),
        },
    )


AGENDA_START_PROPERTIES = {0x10C3, 0x66B5, 0x0060}
AGENDA_END_PROPERTIES = {0x10C4, 0x66B6, 0x0061}


def pff_agenda_datetime(msg, property_ids):
    """Return an agenda datetime from calendar-specific MAPI properties."""
    parsed = normalize_message_datetime(record_value(msg, property_ids))
    if parsed is not None:
        return parsed
    return None


def pff_agenda_status(item_class):
    """Return a friendly response/status label for a meeting class."""
    lowered = s(item_class).lower()
    if "resp.neg" in lowered or "declin" in lowered or "refus" in lowered:
        return "Declined/Refused"
    if "resp.pos" in lowered or "accept" in lowered:
        return "Accepted"
    if "resp.tent" in lowered or "tent" in lowered:
        return "Tentative"
    if "canceled" in lowered or "cancelled" in lowered:
        return "Cancelled"
    if "request" in lowered:
        return "Request"
    return ""


def is_pff_agenda_class(item_class):
    """Return whether a PFF item class belongs in the agenda."""
    lowered = s(item_class).lower()
    return lowered.startswith("ipm.appointment") or lowered.startswith("ipm.schedule")


def safe_export_name(value, fallback="email", limit=80):
    """Return a zip-path-safe filename component."""
    text = re.sub(r"\s+", " ", s(value)).strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.strip(" ._")
    if len(text) > limit:
        text = text[:limit].rstrip(" ._")
    return text or fallback


def export_folder_name(index, subject, date_value):
    """Return a stable subfolder name for an exported email."""
    parsed = normalize_message_datetime(date_value)
    date_part = parsed.strftime("%Y-%m-%d_%H%M") if parsed is not None else "unknown-date"
    return f"{index:03d}_{date_part}-{safe_export_name(subject)}"


def is_checked_state(value):
    """Return whether a Qt check-state value is checked."""
    checked = getattr(Qt.Checked, "value", Qt.Checked)
    state = getattr(value, "value", value)
    return state == checked


def exported_html_document(summary_rows, body_html):
    """Return a single report-friendly HTML document."""
    summary = "".join(
        "<tr>"
        f"<th>{html_lib.escape(s(label))}</th>"
        f"<td>{html_lib.escape(s(value))}</td>"
        "</tr>"
        for label, value in summary_rows
        if s(value)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>"
        "body{background:#fff;color:#111;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.35;}"
        "table.summary{border-collapse:collapse;margin:0 0 16px 0;width:100%;}"
        "table.summary th{background:#f0f0f0;text-align:left;width:180px;vertical-align:top;}"
        "table.summary th,table.summary td{border:1px solid #c8c8c8;padding:5px 8px;}"
        "pre{white-space:pre-wrap;font-family:Arial,Helvetica,sans-serif;}"
        "hr{border:0;border-top:1px solid #999;margin:18px 0;}"
        "</style></head><body>"
        "<h2>Message Summary</h2>"
        f"<table class='summary'>{summary}</table>"
        "<hr />"
        f"{body_html}"
        "</body></html>"
    )


OUTLOOK_ITEM_TYPES = {
    "IPM.Schedule.Meeting.Request": ("Meeting Request", "📨"),
    "IPM.Schedule.Meeting.Resp.Pos": ("Meeting Accepted", "✅"),
    "IPM.Schedule.Meeting.Resp.Neg": ("Meeting Declined", "❌"),
    "IPM.Schedule.Meeting.Resp.Tent": ("Meeting Tentative", "❓"),
    "IPM.Schedule.Meeting.Canceled": ("Meeting Cancelled", "🚫"),
    "IPM.Schedule": ("Scheduling Item", "📅"),
    "IPM.Appointment": ("Appointment", "📅"),
    "IPM.Sharing": ("Sharing Invitation", "🤝"),
    "IPM.Contact": ("Contact", "👤"),
    "IPM.DistList": ("Distribution List", "👥"),
    "IPM.TaskRequest.Accept": ("Task Accepted", "✅"),
    "IPM.TaskRequest.Decline": ("Task Declined", "❌"),
    "IPM.TaskRequest.Update": ("Task Updated", "🔄"),
    "IPM.TaskRequest": ("Task Request", "📋"),
    "IPM.Task": ("Task", "☑️"),
    "IPM.StickyNote": ("Sticky Note", "🗒️"),
    "IPM.Activity": ("Journal Entry", "📓"),
    "IPM.Post": ("Post", "📝"),
    "IPM.Document": ("Document", "📄"),
    "IPM.Note.SMIME": ("Secure Email", "🔐"),
    "IPM.Note.Microsoft.Voicemail": ("Voicemail", "📞"),
    "IPM.Note": ("Email", "📧"),
    "REPORT.IPM.Note": ("Mail Report", "📬"),
    "IPM.Recall.Report": ("Recall Report", "↩️"),
    "*": ("Unknown Outlook Item", "📦"),
}
OUTLOOK_ITEM_PREFIXES = sorted(
    (prefix for prefix in OUTLOOK_ITEM_TYPES if prefix != "*"),
    key=len,
    reverse=True,
)


def outlook_item_type_info(msg):
    """Return Outlook item type info."""
    if isinstance(msg, PffMessageRow):
        return msg.type_label, msg.type_icon, msg.item_class
    item_class = message_class(msg)
    for prefix in OUTLOOK_ITEM_PREFIXES:
        if item_class.startswith(prefix):
            label, emoji = OUTLOOK_ITEM_TYPES[prefix]
            return label, emoji, item_class
    label, emoji = OUTLOOK_ITEM_TYPES["*"]
    return label, emoji, item_class


@dataclass
class PffMessageRow:
    """Stable table-facing snapshot for one libpff message."""

    pff_message: object
    source_index: int
    type_label: str
    type_icon: str
    item_class: str
    subject: str
    contact: str
    sender: str
    to: str
    cc: str
    bcc: str
    classification: str
    classification_color: str
    date: object
    date_sort_kind: str
    date_sort_value: object
    attachment_count: int
    attachment_indicators: list
    size: int
    folder_key: str = ""
    folder_name: str = ""
    search_text: str = ""


def raw_pff_message(row_or_msg):
    """Return the backing libpff message for table rows or raw messages."""
    return row_or_msg.pff_message if isinstance(row_or_msg, PffMessageRow) else row_or_msg


def build_message_row(msg, source_index, contact_cache=None, folder_key="", folder_name=""):
    """Build a stable table row from a libpff message."""
    item_class = message_class(msg)
    label, icon, _item_class = outlook_item_type_info(msg)
    classification, color = email_classification_info(msg)
    kind, sort_value = message_date_sort_key(msg)
    size = int(message_size(msg) or 0)
    count = attachment_count(msg)
    try:
        count = max(count, exposed_attachment_row_count(msg))
    except Exception:
        pass
    return PffMessageRow(
        pff_message=msg,
        source_index=source_index,
        type_label=label,
        type_icon=icon,
        item_class=item_class,
        subject=s(get_value(msg, "subject", "get_subject")) or "(no subject)",
        contact=contact_display(msg),
        sender=resolve_sender(msg, contact_cache),
        to=resolve_to(msg, contact_cache),
        cc=resolve_cc(msg, contact_cache),
        bcc=resolve_bcc(msg, contact_cache),
        classification=classification,
        classification_color=color,
        date=message_date(msg),
        date_sort_kind=kind,
        date_sort_value=sort_value,
        attachment_count=count,
        attachment_indicators=attachment_indicator_details(msg),
        size=size,
        folder_key=folder_key,
        folder_name=folder_name,
    )


class MsgModel(QAbstractTableModel):
    HEADERS = ["Export", "Type", "Subject", "Folder", "Contact", "From", "To", "CC", "BCC", "Classification", "Date", "# Attachments", "Email Size"]
    EXPORT_COLUMN = 0
    TYPE_COLUMN = 1
    SUBJECT_COLUMN = 2
    FOLDER_COLUMN = 3
    CONTACT_COLUMN = 4
    ADDRESS_COLUMNS = {5, 6, 7, 8}
    BCC_COLUMN = 8
    CLASSIFICATION_COLUMN = 9
    DATE_COLUMN = 10
    ATTACHMENT_COLUMN = 11
    EMAIL_SIZE_COLUMN = 12

    def __init__(self):
        """Initialize the instance and its default state."""
        super().__init__()
        self.all_messages = []
        self.messages = []
        self.sort_descending = True
        self.sort_column = self.DATE_COLUMN
        self.sort_order = Qt.DescendingOrder
        self.search_text_cache = {}
        self.classification_cache = {}
        self.size_cache = {}
        self.attachment_count_cache = {}
        self.attachment_indicator_cache = {}
        self.visible_max_size = 0
        self.visible_max_attachments = 0
        self.contact_cache = {}
        self.checked_message_ids = set()

    def set_contact_cache(self, contact_cache):
        """Set contact cache."""
        self.contact_cache = contact_cache or {}
        self.search_text_cache.clear()

    def set(self, msgs):
        """Replace the model contents and refresh visible state."""
        self.replace_all(msgs)

    def replace_all(self, msgs):
        """Replace the model contents and refresh visible state."""
        self.beginResetModel()
        self.search_text_cache.clear()
        self.classification_cache.clear()
        self.size_cache.clear()
        self.attachment_count_cache.clear()
        self.attachment_indicator_cache.clear()
        self.all_messages = msgs
        self.messages = self.sorted_messages(msgs)
        self.visible_max_size = self.max_message_size(self.messages)
        self.visible_max_attachments = self.max_attachment_count(self.messages)
        self.endResetModel()

    def refresh_loaded(self, all_msgs, visible_msgs):
        """Refresh loaded messages while appending when possible to avoid flicker."""
        sorted_visible = self.sorted_messages(visible_msgs)
        if self.can_append_visible(sorted_visible):
            start = len(self.messages)
            end = len(sorted_visible) - 1
            self.all_messages = all_msgs
            if start <= end:
                self.beginInsertRows(QModelIndex(), start, end)
                self.messages.extend(sorted_visible[start:])
                self.endInsertRows()
            self.visible_max_size = self.max_message_size(self.messages)
            self.visible_max_attachments = self.max_attachment_count(self.messages)
            if self.messages:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(self.messages) - 1, len(self.HEADERS) - 1),
                )
            return

        self.replace_all(visible_msgs)
        self.all_messages = all_msgs

    def can_append_visible(self, sorted_visible):
        """Return whether a visible refresh can be represented as row appends."""
        if len(sorted_visible) < len(self.messages):
            return False
        return sorted_visible[:len(self.messages)] == self.messages

    def set_visible(self, msgs):
        """Set visible."""
        self.beginResetModel()
        self.messages = self.sorted_messages(msgs)
        self.visible_max_size = self.max_message_size(self.messages)
        self.visible_max_attachments = self.max_attachment_count(self.messages)
        self.endResetModel()

    def set_sort_descending(self, descending):
        """Set sort descending."""
        if self.sort_descending == descending:
            return
        self.sort_descending = descending
        self.beginResetModel()
        self.messages = self.sorted_messages(self.messages)
        self.visible_max_size = self.max_message_size(self.messages)
        self.visible_max_attachments = self.max_attachment_count(self.messages)
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        """Sort table rows by a clicked header column."""
        if not (0 <= column < len(self.HEADERS)):
            return
        self.sort_column = column
        self.sort_order = order
        self.sort_descending = order == Qt.DescendingOrder
        self.beginResetModel()
        self.messages = self.sorted_messages(self.messages)
        self.visible_max_size = self.max_message_size(self.messages)
        self.visible_max_attachments = self.max_attachment_count(self.messages)
        self.endResetModel()

    def message_size_value(self, msg):
        """Return message size value."""
        if isinstance(msg, PffMessageRow):
            return msg.size
        cache_key = id(msg)
        if cache_key not in self.size_cache:
            try:
                self.size_cache[cache_key] = int(message_size(msg) or 0)
            except Exception:
                self.size_cache[cache_key] = 0
        return self.size_cache[cache_key]

    def max_message_size(self, msgs):
        """Return the largest visible message size for heat-map scaling."""
        return max((self.message_size_value(msg) for msg in msgs), default=0)

    def attachment_count_value(self, msg):
        """Return cached attachment count for a message."""
        if isinstance(msg, PffMessageRow):
            return msg.attachment_count
        cache_key = id(msg)
        if cache_key not in self.attachment_count_cache or self.attachment_count_cache[cache_key] == 0:
            # Some libpff builds lazily materialize attachment tables. Avoid
            # making an early zero sticky for the lifetime of the model.
            count = attachment_count(msg)
            if count == 0 or self.message_size_value(msg) > 10 * 1024 * 1024:
                try:
                    count = max(count, exposed_attachment_count(msg))
                except Exception:
                    pass
            self.attachment_count_cache[cache_key] = count
        return self.attachment_count_cache[cache_key]

    def attachment_indicator_details(self, msg):
        """Return cached attachment marker details for a message."""
        if isinstance(msg, PffMessageRow):
            return msg.attachment_indicators
        cache_key = id(msg)
        if cache_key not in self.attachment_indicator_cache:
            self.attachment_indicator_cache[cache_key] = attachment_indicator_details(msg)
        return self.attachment_indicator_cache[cache_key]

    def has_unexposed_attachment_indicators(self, msg):
        """Return whether MAPI hints at attachments hidden from libpff rows."""
        return self.attachment_count_value(msg) == 0 and bool(self.attachment_indicator_details(msg))

    def max_attachment_count(self, msgs):
        """Return the largest visible attachment count for heat-map scaling."""
        return max((self.attachment_count_value(msg) for msg in msgs), default=0)

    def sorted_messages(self, msgs):
        """Return messages sorted by the active column and direction."""
        reverse = self.sort_order == Qt.DescendingOrder
        indexed = list(enumerate(msgs))
        indexed.sort(key=lambda item: self.sort_key(item[1], item[0]), reverse=reverse)
        return [msg for _index, msg in indexed]

    def sort_key(self, msg, original_index):
        """Return a stable, column-aware sort key for a message."""
        column = self.sort_column
        if column == self.EXPORT_COLUMN:
            return (self.selection_key(msg) in self.checked_message_ids, original_index)
        if column == self.TYPE_COLUMN:
            return (outlook_item_type_info(msg)[0].lower(), original_index)
        if column == self.SUBJECT_COLUMN:
            return ((msg.subject if isinstance(msg, PffMessageRow) else s(get_value(msg, "subject", "get_subject"))).lower(), original_index)
        if column == self.CONTACT_COLUMN:
            return ((msg.contact if isinstance(msg, PffMessageRow) else contact_display(msg)).lower(), original_index)
        if column == self.FOLDER_COLUMN:
            return (msg.folder_name.lower() if isinstance(msg, PffMessageRow) else "", original_index)
        if column in self.ADDRESS_COLUMNS:
            return (self.address_value(msg, column).lower(), original_index)
        if column == self.CLASSIFICATION_COLUMN:
            return (self.classification(msg).lower(), original_index)
        if column == self.DATE_COLUMN:
            if isinstance(msg, PffMessageRow):
                kind, value = msg.date_sort_kind, msg.date_sort_value
            else:
                kind, value = message_date_sort_key(msg)
            kind_rank = {"date": 2, "text": 1}.get(kind, 0)
            return (kind_rank, value, original_index)
        if column == self.ATTACHMENT_COLUMN:
            return (self.attachment_count_value(msg), int(self.has_unexposed_attachment_indicators(msg)), original_index)
        if column == self.EMAIL_SIZE_COLUMN:
            return (self.message_size_value(msg), original_index)
        return ("", original_index)

    def searchable_text(self, msg):
        """Return cached lowercase text used by message search."""
        cache_key = id(msg)
        if cache_key not in self.search_text_cache:
            if isinstance(msg, PffMessageRow) and msg.search_text:
                self.search_text_cache[cache_key] = msg.search_text
                return self.search_text_cache[cache_key]
            raw_msg = raw_pff_message(msg)
            html, text, rtf = resolve_body(raw_msg)
            self.search_text_cache[cache_key] = "\n".join((
                msg.item_class if isinstance(msg, PffMessageRow) else message_class(msg),
                outlook_item_type_info(msg)[0],
                msg.subject if isinstance(msg, PffMessageRow) else s(get_value(msg, "subject", "get_subject")),
                msg.contact if isinstance(msg, PffMessageRow) else contact_display(msg),
                msg.sender if isinstance(msg, PffMessageRow) else resolve_sender(msg, self.contact_cache),
                msg.to if isinstance(msg, PffMessageRow) else resolve_to(msg, self.contact_cache),
                msg.cc if isinstance(msg, PffMessageRow) else resolve_cc(msg, self.contact_cache),
                msg.bcc if isinstance(msg, PffMessageRow) else resolve_bcc(msg, self.contact_cache),
                self.classification(msg),
                msg.folder_name if isinstance(msg, PffMessageRow) else "",
                strip_html(html),
                text,
                rtf_to_text(rtf),
            ))
        return self.search_text_cache[cache_key]

    def classification(self, msg):
        """Return a message classification label."""
        if isinstance(msg, PffMessageRow):
            return msg.classification
        return self.classification_info(msg)[0]

    def classification_color(self, msg):
        """Return the display color for a message classification."""
        if isinstance(msg, PffMessageRow):
            return msg.classification_color
        return self.classification_info(msg)[1]

    def classification_info(self, msg):
        """Return the classification label and color for a message."""
        cache_key = id(msg)
        if cache_key not in self.classification_cache:
            self.classification_cache[cache_key] = email_classification_info(msg)
        return self.classification_cache[cache_key]

    def has_contacts(self):
        """Return whether contacts."""
        return any((msg.contact if isinstance(msg, PffMessageRow) else contact_display(msg)) for msg in self.messages)

    def has_bcc(self):
        """Return whether bcc."""
        return any((msg.bcc if isinstance(msg, PffMessageRow) else resolve_bcc(msg, self.contact_cache)) for msg in self.messages)

    def address_value(self, msg, column):
        """Return the address text for a message table address column."""
        if column == 5:
            return msg.sender if isinstance(msg, PffMessageRow) else resolve_sender(msg, self.contact_cache)
        if column == 6:
            return msg.to if isinstance(msg, PffMessageRow) else resolve_to(msg, self.contact_cache)
        if column == 7:
            return msg.cc if isinstance(msg, PffMessageRow) else resolve_cc(msg, self.contact_cache)
        if column == self.BCC_COLUMN:
            return msg.bcc if isinstance(msg, PffMessageRow) else resolve_bcc(msg, self.contact_cache)
        return ""

    def rowCount(self, parent=QModelIndex()):
        """Return the number of table rows for Qt."""
        return len(self.messages)

    def columnCount(self, parent=QModelIndex()):
        """Return the number of table columns for Qt."""
        return len(self.HEADERS)

    def headerData(self, section, orientation, role):
        """Return header text for the Qt table model."""
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return self.HEADERS[section]
        return None

    def selection_key(self, msg):
        """Return stable selection key for a message."""
        if isinstance(msg, PffMessageRow) and msg.folder_key:
            return f"{msg.folder_key}:{msg.source_index}"
        raw_msg = raw_pff_message(msg)
        return id(raw_msg)

    def selected_messages(self):
        """Return checked messages in loaded order."""
        selected = []
        seen = set()
        sources = []
        owner = getattr(self, "owner", None)
        if owner is not None:
            sources.extend(owner.checked_message_store.values())
        sources.extend(list(self.all_messages) + list(self.messages))
        for msg in sources:
            key = self.selection_key(msg)
            if key in self.checked_message_ids and key not in seen:
                selected.append(msg)
                seen.add(key)
        return selected

    def clear_selection(self):
        """Clear checked export rows."""
        if not self.checked_message_ids:
            owner = getattr(self, "owner", None)
            if owner is not None:
                owner.checked_message_store.clear()
                owner.update_show_selected_button()
            return
        self.checked_message_ids.clear()
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.checked_message_store.clear()
            owner.update_show_selected_button()
        if self.rowCount():
            self.dataChanged.emit(
                self.index(0, self.EXPORT_COLUMN),
                self.index(self.rowCount() - 1, self.EXPORT_COLUMN),
            )

    def flags(self, idx):
        """Return Qt item flags."""
        if idx.isValid() and idx.column() == self.EXPORT_COLUMN:
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        return super().flags(idx)

    def setData(self, idx, value, role=Qt.EditRole):
        """Handle checkbox changes."""
        if not idx.isValid() or idx.column() != self.EXPORT_COLUMN or role != Qt.CheckStateRole:
            return False
        msg = self.messages[idx.row()]
        key = self.selection_key(msg)
        if is_checked_state(value):
            self.checked_message_ids.add(key)
            owner = getattr(self, "owner", None)
            if owner is not None:
                owner.checked_message_store[key] = msg
        else:
            self.checked_message_ids.discard(key)
            owner = getattr(self, "owner", None)
            if owner is not None:
                owner.checked_message_store.pop(key, None)
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.update_show_selected_button()
        self.dataChanged.emit(idx, idx)
        return True

    def data(self, idx, role):
        """Return display, sorting, and styling data for a table cell."""
        if not idx.isValid():
            return None

        m = self.messages[idx.row()]

        if idx.column() == self.EXPORT_COLUMN:
            if role == Qt.CheckStateRole:
                return Qt.Checked if self.selection_key(m) in self.checked_message_ids else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""

        # Non-display roles provide tooltips, address highlighting, attachment
        # markers, size heat maps, and classification colors.
        if role == Qt.ToolTipRole and idx.column() == self.TYPE_COLUMN:
            label, _emoji, item_class = outlook_item_type_info(m)
            return f"{label} ({item_class or 'no message class'})"
        if role == Qt.ToolTipRole and idx.column() == self.ATTACHMENT_COLUMN:
            details = self.attachment_indicator_details(m)
            count = self.attachment_count_value(m)
            if count == 0 and details:
                return "Attachment markers found, but libpff exposed no attachment rows:\n" + "\n".join(details)

        if idx.column() in self.ADDRESS_COLUMNS:
            address_value = self.address_value(m, idx.column())
            if contains_personal_email(address_value):
                if role == Qt.BackgroundRole:
                    return personal_address_brush(address_value)
                if role == Qt.ForegroundRole:
                    return dark_text_brush()
            if contains_external_email(address_value):
                if role == Qt.BackgroundRole:
                    return external_address_brush(address_value)
                if role == Qt.ForegroundRole:
                    return dark_text_brush()

        attachment_total = self.attachment_count_value(m) if idx.column() == self.ATTACHMENT_COLUMN else 0
        if idx.column() == self.ATTACHMENT_COLUMN and attachment_total > 0 and self.visible_max_attachments > 0:
            if role == Qt.BackgroundRole:
                background, _foreground = heat_brushes(
                    attachment_total,
                    self.visible_max_attachments,
                )
                return background
            if role == Qt.ForegroundRole:
                return dark_text_brush()
        if idx.column() == self.ATTACHMENT_COLUMN and self.has_unexposed_attachment_indicators(m):
            if role == Qt.BackgroundRole:
                return QBrush(QColor("#f0c36a"))
            if role == Qt.ForegroundRole:
                return dark_text_brush()

        if idx.column() == self.EMAIL_SIZE_COLUMN and self.visible_max_size > 0:
            if role == Qt.BackgroundRole:
                background, _foreground = heat_brushes(self.message_size_value(m), self.visible_max_size)
                return background
            if role == Qt.ForegroundRole:
                return dark_text_brush()

        if role == Qt.BackgroundRole and idx.column() == self.CLASSIFICATION_COLUMN:
            color = QColor(self.classification_color(m))
            if color.isValid():
                return QBrush(color)
            return None

        if role == Qt.ForegroundRole and idx.column() == self.CLASSIFICATION_COLUMN:
            color = QColor(self.classification_color(m))
            if color.isValid() and label_text_is_dark(color):
                return QBrush(QColor("#ffffff"))
            return None

        if role != Qt.DisplayRole:
            return None

        # DisplayRole returns the visible cell text after styling roles exit.
        if idx.column() == self.TYPE_COLUMN:
            return outlook_item_type_info(m)[1]
        if idx.column() == self.SUBJECT_COLUMN:
            return m.subject if isinstance(m, PffMessageRow) else s(get_value(m, "subject", "get_subject")) or "(no subject)"
        if idx.column() == self.FOLDER_COLUMN:
            return folder_leaf(m.folder_name) if isinstance(m, PffMessageRow) else ""
        if idx.column() == self.CONTACT_COLUMN:
            return m.contact if isinstance(m, PffMessageRow) else contact_display(m)
        if idx.column() in self.ADDRESS_COLUMNS:
            return self.address_value(m, idx.column())
        if idx.column() == self.CLASSIFICATION_COLUMN:
            return self.classification(m)
        if idx.column() == self.DATE_COLUMN:
            return s(m.date if isinstance(m, PffMessageRow) else message_date(m))
        if idx.column() == self.ATTACHMENT_COLUMN:
            if self.has_unexposed_attachment_indicators(m):
                return "0*"
            return str(self.attachment_count_value(m))
        if idx.column() == self.EMAIL_SIZE_COLUMN:
            return format_bytes(self.message_size_value(m))

        return None

    def msg(self, row):
        """Return the backing message object for a row."""
        return self.messages[row] if 0 <= row < len(self.messages) else None


# =========================================================
# WORKER (WITH PROGRESS)
# =========================================================
class LoadWorker(QObject):
    finished = Signal(int, list)
    progress = Signal(int, int, int)
    partial = Signal(int, list, int, int)
    PARTIAL_REFRESH_SECONDS = 10.0

    def __init__(self, folder, load_id, contact_cache=None, folder_key="", folder_name=""):
        """Initialize the instance and its default state."""
        super().__init__()
        self.folder = folder
        self.load_id = load_id
        self.contact_cache = contact_cache or {}
        self.folder_key = folder_key
        self.folder_name = folder_name

    def run(self):
        """Run the worker task and emit progress/results to the UI thread."""
        msgs = []
        total = folder_message_count(self.folder)
        last_partial = time.monotonic()
        log_event(f"Starting folder parse: load_id={self.load_id}, total_messages={total}")

        for position, i in enumerate(range(total - 1, -1, -1), 1):
            try:
                msg = self.folder.get_sub_message(i)
                msgs.append(build_message_row(msg, i, self.contact_cache, self.folder_key, self.folder_name))
            except Exception as exc:
                debug_exception(f"Unable to read folder message {i}", exc)

            self.progress.emit(self.load_id, position, total)
            now = time.monotonic()
            if len(msgs) and (position == total or now - last_partial >= self.PARTIAL_REFRESH_SECONDS):
                self.partial.emit(self.load_id, list(msgs), position, total)
                last_partial = now

        log_event(f"Finished folder parse: load_id={self.load_id}, messages={len(msgs)}")
        self.finished.emit(self.load_id, msgs)


class TimelineIndexWorker(QObject):
    finished = Signal(int, list, dict, dict, list)
    progress = Signal(int, int, int)

    def __init__(self, root_folder, load_id, total_messages, contact_cache=None):
        """Initialize the instance and its default state."""
        super().__init__()
        self.root_folder = root_folder
        self.load_id = load_id
        self.total_messages = total_messages
        self.contact_cache = contact_cache or {}

    def run(self):
        """Run the worker task and emit progress/results to the UI thread."""
        timeline_values = []
        folder_timestamps = {}
        indexed_messages_by_folder = {}
        agenda_items = []
        processed = 0
        log_event(f"Starting mailbox index: load_id={self.load_id}, total_messages={self.total_messages}")

        def walk(folder, folder_key, folder_label):
            """Walk a folder subtree and collect message timestamps."""
            nonlocal processed
            direct_count = folder_message_count(folder)
            folder_values = []
            folder_rows = []

            for index in range(direct_count):
                try:
                    msg = folder.get_sub_message(index)
                    row = build_message_row(msg, index, self.contact_cache, folder_key, folder_label)
                    row.search_text = build_indexed_search_text(row, msg)
                    folder_rows.append(row)
                    agenda_item = pff_agenda_item_from_row(row, msg, folder_label)
                    if agenda_item is not None:
                        agenda_items.append(agenda_item)
                    parsed = message_effective_datetime(row)
                    if parsed is not None:
                        timeline_values.append(parsed)
                        folder_values.append(parsed)
                except Exception as exc:
                    debug_exception(f"Unable to index folder message {folder_key}:{index}", exc)
                processed += 1
                self.progress.emit(self.load_id, processed, self.total_messages)

            folder_timestamps[folder_key] = folder_values
            indexed_messages_by_folder[folder_key] = folder_rows

            for child_index in range(folder_subfolder_count(folder)):
                child = folder_subfolder_at(folder, child_index)
                if child is not None:
                    child_name = folder_name(child)
                    child_label = f"{folder_label}/{child_name}" if folder_label else child_name
                    walk(child, f"{folder_key}/{child_index}", child_label)

        walk(self.root_folder, "root", folder_name(self.root_folder))
        log_event(
            f"Finished mailbox index: load_id={self.load_id}, "
            f"timestamps={len(timeline_values)}, folders={len(folder_timestamps)}, "
            f"indexed_messages={sum(len(rows) for rows in indexed_messages_by_folder.values())}, "
            f"agenda_items={len(agenda_items)}"
        )
        self.finished.emit(self.load_id, timeline_values, folder_timestamps, indexed_messages_by_folder, agenda_items)


class FolderTreeWorker(QObject):
    finished = Signal(int, object, object, float)
    nodeDiscovered = Signal(int, object)
    autoLightMode = Signal(int, str, float)

    def __init__(self, root_folder, opened_file_name, folder_count_cache, load_id, load_counts=True):
        """Initialize the instance and its default state."""
        super().__init__()
        self.root_folder = root_folder
        self.opened_file_name = opened_file_name
        self.folder_count_cache = dict(folder_count_cache or {})
        self.load_id = load_id
        self.load_counts = load_counts
        self.auto_light_emitted = False

    def maybe_emit_auto_light(self, started):
        """Emit the auto-light signal once folder enumeration exceeds the budget."""
        elapsed = time.monotonic() - started
        if not self.auto_light_emitted and elapsed > AUTO_LIGHT_LOAD_SECONDS:
            self.auto_light_emitted = True
            self.autoLightMode.emit(self.load_id, "folder tree load", elapsed)

    def run(self):
        """Build folder tree data without touching Qt widgets."""
        started = time.monotonic()
        folder_direct_counts = {}
        folder_count_cache = dict(self.folder_count_cache)
        log_event("Starting folder tree worker")

        def add(folder, folder_key="root", is_root=False):
            """Return serializable tree data for a folder and its descendants."""
            self.maybe_emit_auto_light(started)
            if self.load_counts:
                raw_direct_count = folder_message_count(folder)
                cached_direct_count = folder_count_cache.get(folder_key)
                if raw_direct_count == 0 and cached_direct_count:
                    direct_count = cached_direct_count
                else:
                    direct_count = raw_direct_count
                    folder_count_cache[folder_key] = direct_count
                count_known = True
            else:
                direct_count = folder_count_cache.get(folder_key)
                count_known = direct_count is not None
                if direct_count is None:
                    direct_count = 0
            recursive_count = direct_count
            display_name = self.opened_file_name if is_root and self.opened_file_name else folder_name(folder)
            children = []
            try:
                subfolder_count = folder_subfolder_count(folder)
            except Exception:
                subfolder_count = 0
            self.nodeDiscovered.emit(self.load_id, {
                "folder": folder,
                "folder_key": folder_key,
                "parent_key": folder_key.rsplit("/", 1)[0] if "/" in folder_key else None,
                "display_name": display_name,
                "direct_count": direct_count,
                "recursive_count": direct_count,
                "count_known": count_known,
                "child_count": subfolder_count,
            })

            for index in range(subfolder_count):
                self.maybe_emit_auto_light(started)
                subfolder = folder_subfolder_at(folder, index)
                if subfolder is None:
                    continue
                child = add(subfolder, f"{folder_key}/{index}")
                recursive_count += child["recursive_count"]
                children.append(child)

            folder_direct_counts[folder_key] = direct_count
            return {
                "folder": folder,
                "folder_key": folder_key,
                "display_name": display_name,
                "direct_count": direct_count,
                "recursive_count": recursive_count,
                "count_known": count_known,
                "children": children,
            }

        tree_data = add(self.root_folder, "root", True)
        elapsed = time.monotonic() - started
        log_event(
            f"Finished folder tree worker: folders={len(folder_direct_counts)}, "
            f"direct_messages={sum(folder_direct_counts.values())}, elapsed={elapsed:.1f}s"
        )
        self.finished.emit(
            self.load_id,
            tree_data,
            {
                "folder_direct_counts": folder_direct_counts,
                "folder_count_cache": folder_count_cache,
            },
            elapsed,
        )


def folder_count_colors(count, scale_max):
    """Return folder count colors."""
    return heat_brushes(count, scale_max)


def style_folder_count_cell(item, column, count, scale_max):
    """Style folder count cell."""
    item.setTextAlignment(column, Qt.AlignCenter)
    item.setBackground(column, QBrush())
    item.setForeground(column, QBrush())
    if count == 0:
        return
    background, foreground = folder_count_colors(count, scale_max)
    item.setBackground(column, background)
    item.setForeground(column, foreground)


def style_folder_name_cell(item, folder_label):
    """Style a folder tree name cell with the timeline folder color."""
    color = folder_color(folder_label)
    item.setBackground(0, QBrush(color))
    item.setForeground(0, dark_text_brush() if color.lightness() > 135 else QBrush(QColor("#F8FAFC")))


class SortableTreeWidgetItem(QTreeWidgetItem):
    """Tree item that sorts folder count columns numerically."""

    NUMERIC_COLUMNS = {1, 2, 3, 4}

    def __lt__(self, other):
        """Compare rows using numeric values for count columns."""
        column = self.treeWidget().sortColumn()
        left_empty = bool(getattr(self, "folder_empty", False))
        right_empty = bool(getattr(other, "folder_empty", False))
        if left_empty != right_empty:
            return left_empty and not right_empty
        if column in self.NUMERIC_COLUMNS:
            try:
                left = int(self.text(column) or 0)
            except ValueError:
                left = 0
            try:
                right = int(other.text(column) or 0)
            except ValueError:
                right = 0
            return left < right
        return self.text(column).lower() < other.text(column).lower()


def beautify_html_source(source):
    """Return a readable representation of rendered HTML source."""
    text = s(source).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    text = re.sub(r">\s*<", ">\n<", text)
    lines = []
    indent = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        is_closing = bool(re.match(r"^</", line))
        is_comment = line.startswith("<!--")
        is_doctype = bool(re.match(r"^<!doctype", line, flags=re.IGNORECASE))
        is_self_closing = bool(re.search(r"/>\s*$", line))
        tag_match = re.match(r"^<\s*([A-Za-z][A-Za-z0-9:_-]*)\b", line)
        tag_name = tag_match.group(1).lower() if tag_match else ""
        is_void = tag_name in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }

        if is_closing:
            indent = max(indent - 1, 0)
        lines.append(("  " * indent) + line)
        opens_tag = tag_match and not is_closing and not is_self_closing and not is_void and not is_comment and not is_doctype
        closes_same_line = bool(re.search(r"</\s*" + re.escape(tag_name) + r"\s*>\s*$", line, flags=re.IGNORECASE))
        if opens_tag and not closes_same_line:
            indent += 1

    return "\n".join(lines)


class HtmlSourceHighlighter(QSyntaxHighlighter):
    """Apply lightweight HTML syntax highlighting to the rendered source tab."""

    def __init__(self, document):
        """Initialize highlighting formats."""
        super().__init__(document)
        self.tag_format = QTextCharFormat()
        self.tag_format.setForeground(QColor("#7cc7ff"))
        self.attribute_format = QTextCharFormat()
        self.attribute_format.setForeground(QColor("#f0c36a"))
        self.value_format = QTextCharFormat()
        self.value_format.setForeground(QColor("#9cdc8f"))
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor("#8a8a8a"))
        self.entity_format = QTextCharFormat()
        self.entity_format.setForeground(QColor("#d7aefb"))

    def highlightBlock(self, text):
        """Highlight one line of HTML source."""
        for match in re.finditer(r"<!--.*?-->", text):
            self.setFormat(match.start(), match.end() - match.start(), self.comment_format)

        for match in re.finditer(r"&[A-Za-z0-9#]+;", text):
            self.setFormat(match.start(), match.end() - match.start(), self.entity_format)

        for tag in re.finditer(r"</?\s*[A-Za-z][A-Za-z0-9:_-]*|/?>", text):
            self.setFormat(tag.start(), tag.end() - tag.start(), self.tag_format)

        for attr in re.finditer(r"\b[A-Za-z_:][-A-Za-z0-9_:.]*(?=\s*=)", text):
            self.setFormat(attr.start(), attr.end() - attr.start(), self.attribute_format)

        for value in re.finditer(r"(['\"]).*?\1", text):
            self.setFormat(value.start(), value.end() - value.start(), self.value_format)


class JsonSourceHighlighter(QSyntaxHighlighter):
    """Apply lightweight JSON syntax highlighting to the JSON tab."""

    def __init__(self, document):
        """Initialize highlighting formats."""
        super().__init__(document)
        self.key_format = QTextCharFormat()
        self.key_format.setForeground(QColor("#7cc7ff"))
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor("#9cdc8f"))
        self.number_format = QTextCharFormat()
        self.number_format.setForeground(QColor("#d7aefb"))
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(QColor("#f0c36a"))
        self.punctuation_format = QTextCharFormat()
        self.punctuation_format.setForeground(QColor("#c8c8c8"))

    def highlightBlock(self, text):
        """Highlight one line of JSON source."""
        for match in re.finditer(r"[{}\[\],:]", text):
            self.setFormat(match.start(), match.end() - match.start(), self.punctuation_format)

        for match in re.finditer(r"\b(?:true|false|null)\b", text):
            self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)

        for match in re.finditer(r"(?<![\w.])-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

        for match in re.finditer(r'"(?:\\.|[^"\\])*"', text):
            fmt = self.key_format if re.match(r'\s*:', text[match.end():]) else self.string_format
            self.setFormat(match.start(), match.end() - match.start(), fmt)


# =========================================================
# MAIN WINDOW
# =========================================================
class OSTViewer(QMainWindow):
    def __init__(self, initial_file=None, light_load_mode=False):
        """Initialize the instance and its default state."""
        super().__init__()
        self.setWindowTitle("PFF (OST/PST) Viewer")
        apply_qt_app_icon(self)
        self.resize(1400, 850)

        self.reader = None
        self.thread = None
        self.worker = None
        self.timeline_thread = None
        self.timeline_worker = None
        self.tree_thread = None
        self.tree_worker = None
        self.load_id = 0
        self.timeline_load_id = 0
        self.tree_load_id = 0
        self.active_loads = {}
        self.active_timeline_loads = {}
        self.active_tree_loads = {}
        self.current_attachments = []
        self.current_property_entries = []
        self.contact_cache = {}
        self.opened_file_name = ""
        self.time_filter = None
        self.text_filter = ""
        self.current_folder_key = None
        self.current_folder_loaded_key = None
        self.current_folder_messages = []
        self.loaded_folder_messages_by_key = {}
        self.current_folder_name = ""
        self.checked_message_store = {}
        self.agenda_text_filter = ""
        self.folder_direct_counts = {}
        self.folder_count_cache = {}
        self.folder_items_by_key = {}
        self.pending_folder_nodes = {}
        self.folder_timestamps = {}
        self.indexed_messages_by_folder = {}
        self.indexed_all_messages = []
        self.global_search_index_ready = False
        self.timeline_index_ready = False
        self.mailbox_index_started_at = None
        self.mailbox_index_progress_samples = deque()
        self.light_load_mode = light_load_mode
        self.opening_file = False
        self.reload_current_folder_after_tree = False

        self._ui()
        self.apply_light_load_ui_state()

        if initial_file:
            self.open_file(initial_file)

    # ---------------- UI ----------------
    def _ui(self):

        # MENU BAR (RESTORED)
        """Build and connect the window user interface."""
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        open_action = file_menu.addAction("Open OST/PST")
        open_action.triggered.connect(self.open_dialog)
        filter_menu = menubar.addMenu("Filter")
        self.clear_time_filter_action = filter_menu.addAction("Clear Time Filter")
        self.clear_time_filter_action.triggered.connect(self.clear_time_filter)
        options_menu = menubar.addMenu("Options")
        self.light_load_action = options_menu.addAction("Low disk I/O mode")
        self.light_load_action.setCheckable(True)
        self.light_load_action.setChecked(self.light_load_mode)
        self.light_load_action.setToolTip("Skip global PST/OST timeline and recipient-cache scans; load messages only when a folder is selected.")
        self.light_load_action.toggled.connect(self.set_light_load_mode)

        # TREE
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Folders", "Items", "Total", "Included", "Excluded"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(2, Qt.DescendingOrder)
        self.tree.itemClicked.connect(self.load_folder)

        # TABLE
        self.model = MsgModel()
        self.model.owner = self

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setColumnWidth(MsgModel.EXPORT_COLUMN, 58)
        self.table.setColumnWidth(MsgModel.TYPE_COLUMN, 44)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(MsgModel.DATE_COLUMN, Qt.DescendingOrder)

        self.table.selectionModel().currentChanged.connect(
            lambda current, previous: self.on_select(current)
        )
        self.update_optional_columns()

        # SEARCH
        # Search is debounced so large folders are not filtered on every keypress.
        self.search = QLineEdit()
        self.search.setPlaceholderText("Regex search across mailbox, e.g. meeting|travel")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.apply_search)
        self.search.textChanged.connect(self.schedule_search)

        self.date_sort_button = QPushButton("Date: Newest")
        self.date_sort_button.setCheckable(True)
        self.date_sort_button.setChecked(True)
        self.date_sort_button.setToolTip("Toggle message date sort order")
        self.date_sort_button.clicked.connect(self.toggle_date_sort)
        self.export_button = QPushButton("Export")
        self.export_button.setToolTip("Export checked emails to a reporting zip")
        self.export_button.clicked.connect(self.export_selected_messages)
        self.show_selected_button = QPushButton("Show selected (0)")
        self.show_selected_button.setToolTip("Show all checked emails remembered across searches and folders")
        self.show_selected_button.clicked.connect(self.show_selected_messages)

        table_controls = QWidget()
        table_controls_layout = QHBoxLayout(table_controls)
        table_controls_layout.setContentsMargins(0, 0, 0, 0)
        table_controls_layout.addWidget(self.search, 1)
        table_controls_layout.addWidget(self.date_sort_button)
        table_controls_layout.addWidget(self.export_button)
        table_controls_layout.addWidget(self.show_selected_button)

        # BODY
        self.body = QTextBrowser()
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.body.setStyleSheet("QTextBrowser { background: #f7f7f7; color: #111111; }")
        self.body.document().setDefaultStyleSheet(
            "html, body, div, p, span, table, td, th, pre { background-color: transparent; color: #111111; }"
            "a { color: #0645ad; }"
        )
        self.timeline = TimelineBarChart()
        self.timeline.timeFilterChanged.connect(self.apply_time_filter)
        self.timeline_index_widget = QWidget()
        self.timeline_index_widget.setMaximumHeight(96)
        self.timeline_index_widget.setMinimumHeight(56)
        timeline_index_layout = QVBoxLayout(self.timeline_index_widget)
        timeline_index_layout.setContentsMargins(10, 6, 10, 6)
        timeline_index_layout.setSpacing(4)
        self.timeline_index_label = QLabel("Indexing timeline...")
        self.timeline_index_progress = QProgressBar()
        self.timeline_index_progress.setValue(0)
        self.timeline_index_progress.setMaximumHeight(20)
        timeline_index_layout.addWidget(self.timeline_index_label)
        timeline_index_layout.addWidget(self.timeline_index_progress)
        self.timeline_stack = QStackedWidget()
        self.timeline_stack.setMaximumHeight(96)
        self.timeline_stack.setMinimumHeight(56)
        self.timeline_stack.addWidget(self.timeline_index_widget)
        self.timeline_stack.addWidget(self.timeline)
        self.timeline_stack.setCurrentWidget(self.timeline)

        # EMAIL DETAIL TABS
        # Keep summary, raw properties, headers, JSON, and attachments separate
        # while sharing the same selected message.
        self.detail_tabs = QTabWidget()
        self.summary_table = self.make_table(["Field", "Value"])
        self.summary_widget = QSplitter(Qt.Vertical)
        self.summary_widget.addWidget(self.summary_table)
        self.summary_widget.addWidget(self.body)
        self.summary_widget.setStretchFactor(0, 1)
        self.summary_widget.setStretchFactor(1, 4)
        self.headers_table = self.make_table(["PFF .msg Property", "Value", "Size", "Download", "Hexdump"])
        self.headers_table.setWordWrap(False)
        self.headers_table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.headers_table.verticalHeader().setDefaultSectionSize(26)
        self.headers_table.horizontalHeader().setStretchLastSection(False)
        self.headers_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.headers_table.setColumnWidth(0, 220)
        self.headers_table.setColumnWidth(1, 360)
        self.headers_table.setColumnWidth(2, 70)
        self.headers_table.setColumnWidth(3, 90)
        self.headers_table.setColumnWidth(4, 760)
        self.smtp_headers_table = self.make_table(["Header", "Value"])
        self.json_text = QTextEdit()
        self.json_text.setReadOnly(True)
        self.json_text.setLineWrapMode(QTextEdit.NoWrap)
        self.html_source_text = QPlainTextEdit()
        self.html_source_text.setReadOnly(True)
        self.html_source_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.attachments_table = self.make_table(["Name", "Size", "Type", "Save", "Open"])

        self.monospace_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.json_text.setFont(self.monospace_font)
        self.html_source_text.setFont(self.monospace_font)
        self.json_highlighter = JsonSourceHighlighter(self.json_text.document())
        self.html_source_highlighter = HtmlSourceHighlighter(self.html_source_text.document())

        self.detail_tabs.addTab(self.summary_widget, "Summary")
        self.detail_tabs.addTab(self.headers_table, "PFF .msg Properties")
        self.detail_tabs.addTab(self.smtp_headers_table, "SMTP Headers")
        self.detail_tabs.addTab(self.json_text, "JSON")
        self.detail_tabs.addTab(self.html_source_text, "HTML Source")

        # PROGRESS (RESTORED)
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)

        # RIGHT PANEL
        right = QSplitter(Qt.Vertical)
        right.addWidget(table_controls)
        right.addWidget(self.table)
        right.addWidget(self.progress_label)
        right.addWidget(self.progress_bar)
        right.addWidget(self.detail_tabs)
        right.setStretchFactor(1, 4)
        right.setStretchFactor(4, 5)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.tree)
        self.main_splitter.addWidget(right)

        mail_root = QWidget()
        l = QVBoxLayout(mail_root)
        l.addWidget(self.timeline_stack)
        l.addWidget(self.main_splitter, 1)

        self.agenda_model = AgendaModel()
        self.agenda_table = QTableView()
        self.agenda_table.setModel(self.agenda_model)
        self.agenda_table.setSelectionBehavior(QTableView.SelectRows)
        self.agenda_table.setSelectionMode(QTableView.SingleSelection)
        self.agenda_table.setSortingEnabled(True)
        self.agenda_table.sortByColumn(AgendaModel.START_COLUMN, Qt.AscendingOrder)
        self.agenda_table.selectionModel().currentChanged.connect(lambda current, previous: self.on_agenda_select(current))
        self.agenda_search = QLineEdit()
        self.agenda_search.setPlaceholderText("Search agenda...")
        self.agenda_search.textChanged.connect(self.apply_agenda_filter)
        self.agenda_calendar = AgendaTwoWeekView()
        self.agenda_calendar.itemClicked.connect(self.select_agenda_item)
        self.agenda_progress_label = QLabel("")
        self.agenda_progress_bar = QProgressBar()
        self.agenda_progress_bar.setVisible(False)
        self.agenda_detail_table = self.make_table(["Field", "Value"])
        agenda_controls = QWidget()
        agenda_controls_layout = QHBoxLayout(agenda_controls)
        agenda_controls_layout.setContentsMargins(0, 0, 0, 0)
        agenda_controls_layout.addWidget(self.agenda_search, 1)
        agenda_top = QWidget()
        agenda_top_layout = QVBoxLayout(agenda_top)
        agenda_top_layout.addWidget(agenda_controls)
        agenda_list_splitter = QSplitter(Qt.Vertical)
        agenda_list_splitter.addWidget(self.agenda_calendar)
        agenda_list_splitter.addWidget(self.agenda_table)
        agenda_list_splitter.setStretchFactor(0, 3)
        agenda_list_splitter.setStretchFactor(1, 2)
        agenda_list_splitter.setSizes([360, 260])
        agenda_top_layout.addWidget(agenda_list_splitter)
        agenda_top_layout.addWidget(self.agenda_progress_label)
        agenda_top_layout.addWidget(self.agenda_progress_bar)
        agenda_splitter = QSplitter(Qt.Vertical)
        agenda_splitter.addWidget(agenda_top)
        agenda_splitter.addWidget(self.agenda_detail_table)
        agenda_splitter.setStretchFactor(0, 5)
        agenda_splitter.setStretchFactor(1, 2)

        self.view_tabs = QTabWidget()
        self.view_tabs.addTab(mail_root, "Mail")
        self.view_tabs.addTab(agenda_splitter, "Agenda")
        self.setCentralWidget(self.view_tabs)
        self.update_agenda_columns()

    def set_light_load_mode(self, enabled):
        """Toggle low disk I/O mode for the current and next mailbox open."""
        if self.light_load_mode == enabled:
            return
        self.light_load_mode = enabled
        log_event(f"Low disk I/O mode {'enabled' if enabled else 'disabled'}")
        self.apply_light_load_ui_state()
        if enabled:
            self.time_filter = None
            self.timeline_load_id += 1
            self.folder_timestamps = {}
            self.timeline_index_ready = False
            self.timeline.clear_time_filter(emit=False)
            self.timeline.clear_overlay()
            self.timeline.set_timestamps([])
            self.disable_agenda_index_for_low_io()
            if self.reader and not self.opening_file and not self.active_tree_loads:
                self.start_folder_tree_load()
        elif self.reader and not self.opening_file and not self.active_tree_loads:
            self.start_timeline_index()

    def enable_light_load_mode_automatically(self, reason, elapsed, threshold=AUTO_LIGHT_LOAD_SECONDS):
        """Enable low disk I/O mode after a slow preliminary load."""
        if self.light_load_mode:
            return
        log_event(
            f"Auto-enabling low disk I/O mode: {reason} took {elapsed:.1f}s "
            f"(threshold {threshold:.0f}s)"
        )
        self.set_light_load_mode(True)

    def on_tree_auto_light_mode(self, load_id, reason, elapsed):
        """Handle tree worker auto low-I/O threshold events."""
        if load_id != self.tree_load_id:
            return
        self.enable_light_load_mode_automatically(reason, elapsed)

    def apply_light_load_ui_state(self):
        """Apply UI visibility for low disk I/O mode."""
        if hasattr(self, "light_load_action"):
            self.light_load_action.setChecked(self.light_load_mode)
        if hasattr(self, "clear_time_filter_action"):
            self.clear_time_filter_action.setEnabled(not self.light_load_mode)
        if hasattr(self, "timeline_stack"):
            self.timeline_stack.setVisible(not self.light_load_mode)
        if hasattr(self, "tree"):
            self.tree.setColumnHidden(3, self.light_load_mode)
            self.tree.setColumnHidden(4, self.light_load_mode)

    def disable_agenda_index_for_low_io(self):
        """Invalidate agenda indexing and show the low-I/O skipped state."""
        self.agenda_model.set([])
        self.agenda_calendar.set_items([])
        self.set_table_rows(self.agenda_detail_table, [])
        self.agenda_progress_bar.setVisible(False)
        self.agenda_progress_label.setText("Agenda indexing is skipped in low disk I/O mode.")

    def make_table(self, headers):
        """Create a read-only table widget with the requested headers."""
        table = QTableWidget()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        if len(headers) > 1:
            table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
        return table

    def set_table_rows(self, table, rows):
        """Replace a table widget's rows with display-safe values."""
        table.setRowCount(0)
        for row_index, row in enumerate(rows):
            table.insertRow(row_index)
            for col_index, value in enumerate(row):
                item = QTableWidgetItem(short_display(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(row_index, col_index, item)
        table.resizeRowsToContents()

    def update_show_selected_button(self):
        """Refresh the selected-export review button count."""
        if hasattr(self, "show_selected_button"):
            self.show_selected_button.setText(f"Show selected ({len(self.model.checked_message_ids)})")

    def clear_detail_tabs(self):
        """Clear all detail panes for the selected message."""
        self.current_attachments = []
        self.current_property_entries = []
        self.set_table_rows(self.summary_table, [])
        self.populate_properties_table([])
        self.set_table_rows(self.smtp_headers_table, [])
        self.json_text.clear()
        self.html_source_text.clear()
        attachment_tab_index = self.detail_tabs.indexOf(self.attachments_table)
        if attachment_tab_index != -1:
            self.detail_tabs.removeTab(attachment_tab_index)
        self.attachments_table.setRowCount(0)
        self.body.clear()

    def set_html_source(self, html):
        """Display the final HTML source used by the preview pane."""
        self.html_source_text.setPlainText(beautify_html_source(html))

    def update_detail_tabs(self, snapshot, row_msg=None):
        """Populate all detail panes for a selected message."""
        summary_rows = [
            ("Folder", getattr(row_msg, "folder_name", "")),
            ("Subject", snapshot.subject),
            ("Contact", snapshot.contact),
            ("From", snapshot.sender),
            ("To", snapshot.to),
            ("CC", snapshot.cc),
            ("BCC", snapshot.bcc),
            ("Classification", snapshot.classification),
            ("Date", snapshot.date),
        ]
        if snapshot.attachments:
            summary_rows.append(("# Attachments", str(len(snapshot.attachments))))
            self.model.attachment_count_cache[id(snapshot.msg)] = len(snapshot.attachments)
            for row_index, row_msg in enumerate(self.model.messages):
                if raw_pff_message(row_msg) is snapshot.msg:
                    if isinstance(row_msg, PffMessageRow):
                        row_msg.attachment_count = len(snapshot.attachments)
                    self.model.dataChanged.emit(
                        self.model.index(row_index, MsgModel.ATTACHMENT_COLUMN),
                        self.model.index(row_index, MsgModel.ATTACHMENT_COLUMN),
                    )
                    break
        elif snapshot.attachment_indicators:
            summary_rows.append(("Attachment Warning", "Attachment markers found, but libpff exposed no attachment rows."))
            summary_rows.append(("Attachment Markers", "; ".join(snapshot.attachment_indicators)))
        self.set_table_rows(self.summary_table, summary_rows)

        self.populate_properties_table(snapshot.metadata)
        self.populate_smtp_headers_table(snapshot.header_rows)

        description = email_description_from_snapshot(snapshot)
        description["folder"] = s(getattr(row_msg, "folder_name", ""))
        description["folder_key"] = s(getattr(row_msg, "folder_key", ""))
        self.json_text.setPlainText(json.dumps(description, indent=2, ensure_ascii=False))

        self.current_attachments = snapshot.attachments
        attachment_tab_index = self.detail_tabs.indexOf(self.attachments_table)
        if self.current_attachments and attachment_tab_index == -1:
            self.detail_tabs.addTab(self.attachments_table, "Attachments")
        elif not self.current_attachments and attachment_tab_index != -1:
            self.detail_tabs.removeTab(attachment_tab_index)

        self.populate_attachments_table()

    def populate_smtp_headers_table(self, header_rows):
        """Populate smtp headers table."""
        self.set_table_rows(self.smtp_headers_table, header_rows)

    def populate_properties_table(self, entries):
        """Populate the raw PFF property table for a message."""
        self.current_property_entries = list(entries)
        self.headers_table.setRowCount(0)
        for row_index, entry in enumerate(self.current_property_entries):
            self.headers_table.insertRow(row_index)
            label = entry["property_id"]
            if entry["name"]:
                label = f"{label} - {entry['name']}"

            values = [
                (0, label),
                (2, str(entry["size"])),
            ]
            for col_index, value in values:
                item = QTableWidgetItem(s(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.headers_table.setItem(row_index, col_index, item)

            value_text = s(entry["value"])
            hexdump_text = s(entry.get("hexdump", ""))
            self.headers_table.setCellWidget(row_index, 1, self.make_scroll_cell(value_text))

            button = QPushButton("Save")
            button.setEnabled(bool(entry.get("data")))
            button.clicked.connect(lambda checked=False, index=row_index: self.save_property(index))
            self.headers_table.setCellWidget(row_index, 3, button)

            self.headers_table.setCellWidget(row_index, 4, self.make_scroll_cell(hexdump_text, monospace=True))
            self.headers_table.setRowHeight(row_index, self.property_row_height(value_text, hexdump_text))
        self.fit_hexdump_column()

    def make_scroll_cell(self, value, monospace=False):
        """Create a scrollable table cell for long property text."""
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(s(value))
        editor.setLineWrapMode(QTextEdit.NoWrap)
        editor.setFrameStyle(0)
        editor.setContentsMargins(0, 0, 0, 0)
        if monospace:
            editor.setFont(self.monospace_font)
        return editor

    def property_row_height(self, *values):
        """Return an appropriate row height for a property value."""
        max_lines = 1
        has_long_line = False
        for value in values:
            text = s(value)
            if text:
                max_lines = max(max_lines, text.count("\n") + 1)
                has_long_line = has_long_line or any(len(line) > 50 for line in text.splitlines() or [text])
        min_height = 52 if has_long_line else 26
        return min(max(min_height, max_lines * 18 + 8), 100)

    def fit_hexdump_column(self):
        """Resize the hexdump column after property rows are populated."""
        self.headers_table.resizeColumnToContents(4)
        fitted_width = self.headers_table.columnWidth(4)
        if fitted_width:
            self.headers_table.setColumnWidth(4, min(max(fitted_width, 760), 1200))

    def save_property(self, index):
        """Prompt for a destination and save a raw property value."""
        if not (0 <= index < len(self.current_property_entries)):
            return

        entry = self.current_property_entries[index]
        data = entry.get("data", b"")
        if not data:
            message = "This property has no data to save."
            log_error("Unable to save property", message)
            QMessageBox.warning(self, "Unable to save property", message)
            return

        property_id = entry["property_id"].replace("0x", "").lower()
        path, _ = QFileDialog.getSaveFileName(self, "Save PFF Property", f"property-{property_id}.bin")
        if not path:
            return

        try:
            log_event(f"Saving PFF property to: {path} ({len(data)} bytes)")
            with open(path, "wb") as f:
                f.write(data)
            log_event(f"Saved PFF property to: {path}")
        except Exception as exc:
            log_error("Unable to save property", s(exc))
            QMessageBox.critical(self, "Unable to save property", s(exc))

    def populate_attachments_table(self):
        """Populate the attachment table for a selected message."""
        self.attachments_table.setRowCount(0)
        for row_index, info in enumerate(self.current_attachments):
            self.attachments_table.insertRow(row_index)
            values = [
                info["name"],
                "" if info["size"] is None else str(info["size"]),
                info["type"],
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(short_display(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.attachments_table.setItem(row_index, col_index, item)

            button = QPushButton("Save")
            button.clicked.connect(lambda checked=False, index=row_index: self.save_attachment(index))
            self.attachments_table.setCellWidget(row_index, 3, button)

            open_button = QPushButton("Open")
            open_button.clicked.connect(lambda checked=False, index=row_index: self.open_attachment(index))
            self.attachments_table.setCellWidget(row_index, 4, open_button)
        self.attachments_table.resizeRowsToContents()

    def attachment_payload(self, index, action="save"):
        """Return attachment metadata and bytes for a table row."""
        if not (0 <= index < len(self.current_attachments)):
            return None, None

        info = self.current_attachments[index]
        data = attachment_bytes(info["_object"])
        if data is None:
            title = f"Unable to {action} attachment"
            message = "This attachment's bytes were not exposed by the installed PST/OST bindings."
            log_error(title, message)
            QMessageBox.warning(self, title, message)
            return None, None
        return info, data

    def attachment_filename(self, info, index):
        """Return a filesystem-safe attachment filename."""
        filename = s(info.get("name")).replace("\\", "/").split("/")[-1].strip()
        if not filename:
            filename = f"attachment-{index + 1}"
        invalid_chars = '<>:"/\\|?*'
        filename = "".join("_" if char in invalid_chars or ord(char) < 32 else char for char in filename)
        return filename.rstrip(" .") or f"attachment-{index + 1}"

    def save_attachment_bytes(self, path, data):
        """Write attachment bytes to disk."""
        log_event(f"Saving attachment bytes to: {path} ({len(data)} bytes)")
        with open(path, "wb") as f:
            f.write(data)

    def save_attachment(self, index):
        """Prompt for a destination and save an attachment payload."""
        info, data = self.attachment_payload(index, "save")
        if data is None:
            return

        filename = self.attachment_filename(info, index)
        path, _ = QFileDialog.getSaveFileName(self, "Save Attachment", filename)
        if not path:
            return

        try:
            self.save_attachment_bytes(path, data)
            log_event(f"Saved attachment to: {path}")
        except Exception as exc:
            log_error("Unable to save attachment", s(exc))
            QMessageBox.critical(self, "Unable to save attachment", s(exc))

    def open_attachment(self, index):
        """Save an attachment to temp storage and open it with the OS handler."""
        info, data = self.attachment_payload(index, "open")
        if data is None:
            return

        temp_dir = tempfile.mkdtemp(prefix="ost-viewer-attachment-")
        log_event(f"Created temporary attachment folder: {temp_dir}")
        filename = self.attachment_filename(info, index)
        path = os.path.join(temp_dir, filename)
        try:
            self.save_attachment_bytes(path, data)
            TEMP_ATTACHMENT_DIRS.add(temp_dir)
            log_event(f"Created temporary attachment file: {path}")
            self.open_path_with_os_handler(path)
        except Exception as exc:
            log_error("Unable to open attachment", s(exc))
            log_event(f"Deleting temporary attachment folder after open failure: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            QMessageBox.critical(self, "Unable to open attachment", s(exc))

    def open_path_with_os_handler(self, path):
        """Open a file using the platform default handler."""
        if sys.platform.startswith("win"):
            log_event(f"Opening attachment with Windows default handler: {path}")
            os.startfile(path)  # pylint: disable=no-member
            return
        if sys.platform == "darwin":
            log_event(f"Opening attachment with macOS default handler: {path}")
            subprocess.Popen(["open", path])
            return

        if shutil.which("xdg-open"):
            try:
                log_event(f"Opening attachment with xdg-open: {path}")
                subprocess.Popen(["xdg-open", path])
                return
            except OSError:
                pass

        folder = os.path.dirname(path)
        if shutil.which("xdg-open"):
            log_event(f"Opening attachment folder with xdg-open: {folder}")
            subprocess.Popen(["xdg-open", folder])
            return
        if shutil.which("gio"):
            log_event(f"Opening attachment folder with gio: {folder}")
            subprocess.Popen(["gio", "open", folder])
            return

        log_error("No desktop opener found", f"Attachment saved to: {path}")
        QMessageBox.information(
            self,
            "Attachment saved",
            f"Attachment saved to:\n{path}\n\nNo desktop opener was found on this system.",
        )

    # ---------------- OPEN ----------------
    def open_dialog(self):
        """Prompt the user to choose a PST or OST file."""
        path, _ = QFileDialog.getOpenFileName(self, "Open PST/OST", "", "*.pst *.ost")
        if path:
            self.open_file(path)

    def open_file(self, path):
        """Open a mailbox file and reset the UI around its contents."""
        path = normalize_native_path(path)
        log_event(f"Opening PST/OST file: {path}")
        self.opening_file = True
        try:
            log_event("Creating PFF reader")
            reader = create_pff_reader()
            log_event("Starting PFF parse/open")
            started = time.monotonic()
            reader.open(path)
            open_elapsed = time.monotonic() - started
        except Exception as exc:
            self.opening_file = False
            log_error("Unable to open PST/OST", s(exc))
            QMessageBox.critical(self, "Unable to open PST/OST", s(exc))
            return

        log_event(f"Opened PST/OST file: {path}")
        self.reader = reader
        self.opened_file_name = os.path.basename(path)
        if open_elapsed > AUTO_LIGHT_LOAD_OPEN_SECONDS:
            self.enable_light_load_mode_automatically(
                "PFF open",
                open_elapsed,
                AUTO_LIGHT_LOAD_OPEN_SECONDS,
            )
        if self.light_load_mode:
            log_event("Skipping recipient/contact cache in low disk I/O mode")
            self.contact_cache = {}
        else:
            log_event("Building recipient/contact cache")
            started = time.monotonic()
            self.contact_cache, completed = build_recipient_cache(
                reader.get_root_folder(),
                deadline=started + AUTO_LIGHT_LOAD_SECONDS,
                return_completed=True,
            )
            elapsed = time.monotonic() - started
            if completed:
                log_event(f"Built recipient/contact cache: {len(self.contact_cache)} entries")
                if elapsed > AUTO_LIGHT_LOAD_SECONDS:
                    self.enable_light_load_mode_automatically("recipient/contact cache", elapsed)
            else:
                log_event(
                    f"Stopped recipient/contact cache after {elapsed:.1f}s: "
                    f"{len(self.contact_cache)} partial entries"
                )
                self.enable_light_load_mode_automatically("recipient/contact cache", elapsed)
        self.model.set_contact_cache(self.contact_cache)
        self.model.owner = self
        self.model.clear_selection()
        self.time_filter = None
        self.text_filter = ""
        self.current_folder_key = None
        self.current_folder_loaded_key = None
        self.current_folder_messages = []
        self.loaded_folder_messages_by_key = {}
        self.current_folder_name = ""
        self.checked_message_store = {}
        self.agenda_text_filter = ""
        self.folder_count_cache = {}
        self.folder_timestamps = {}
        self.indexed_messages_by_folder = {}
        self.indexed_all_messages = []
        self.global_search_index_ready = False
        self.timeline_index_ready = False
        self.mailbox_index_started_at = None
        self.agenda_search.clear()
        if self.light_load_mode:
            self.disable_agenda_index_for_low_io()
        else:
            self.agenda_model.set([])
            self.agenda_calendar.set_items([])
            self.set_table_rows(self.agenda_detail_table, [])
            self.agenda_progress_bar.setVisible(False)
            self.agenda_progress_label.clear()
        self.timeline.clear_time_filter(emit=False)
        self.timeline.clear_overlay()
        self.timeline.set_timestamps([])
        self.timeline_index_label.setText("Indexing mailbox...")
        self.timeline_index_progress.setValue(0)
        self.timeline_stack.setCurrentWidget(self.timeline_index_widget)
        self.apply_light_load_ui_state()
        self.start_folder_tree_load(after_open=True)

    # ---------------- TREE ----------------
    def start_folder_tree_load(self, after_open=False):
        """Start loading the folder tree without blocking the UI thread."""
        if not self.reader:
            return
        if self.active_tree_loads:
            log_event("Skipping folder tree load because one is already active")
            return
        self.tree_load_id += 1
        load_id = self.tree_load_id
        log_event(f"Starting folder tree load: load_id={load_id}")

        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self.folder_direct_counts = {}
        self.folder_items_by_key = {}
        self.pending_folder_nodes = {}
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Loading folders...")

        thread = QThread()
        worker = FolderTreeWorker(
            self.reader.get_root_folder(),
            self.opened_file_name,
            self.folder_count_cache,
            load_id,
            load_counts=not self.light_load_mode,
        )
        worker.moveToThread(thread)
        self.tree_thread = thread
        self.tree_worker = worker
        self.active_tree_loads[load_id] = (thread, worker, after_open)

        thread.started.connect(worker.run)
        worker.nodeDiscovered.connect(self.on_folder_tree_node_discovered)
        worker.autoLightMode.connect(self.on_tree_auto_light_mode)
        worker.finished.connect(self.on_folder_tree_loaded)
        worker.finished.connect(lambda _load_id, _tree, _state, _elapsed, thread=thread: thread.quit())
        worker.finished.connect(lambda _load_id, _tree, _state, _elapsed, worker=worker: worker.deleteLater())
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda load_id=load_id: self.active_tree_loads.pop(load_id, None))
        thread.start()

    def on_folder_tree_node_discovered(self, load_id, node):
        """Add one discovered folder to the tree while enumeration continues."""
        if load_id != self.tree_load_id:
            return
        self.add_progressive_folder_node(node)
        if not self.active_loads:
            found = len(self.folder_items_by_key)
            self.progress_label.setText(f"Loading folders... {found} found")

    def add_progressive_folder_node(self, node):
        """Add or update a single folder tree node."""
        folder_key = node["folder_key"]
        existing = self.folder_items_by_key.get(folder_key)
        direct_count = node["direct_count"]
        count_known = node.get("count_known", True)
        recursive_count = node.get("recursive_count", direct_count)
        folder_empty = count_known and recursive_count == 0
        included_count, excluded_count = self.folder_filter_counts(folder_key, direct_count)
        direct_text = str(direct_count) if count_known else ""
        recursive_text = str(recursive_count) if count_known else ""
        included_text = str(included_count) if count_known else ""
        excluded_text = str(excluded_count) if count_known else ""

        if existing is None:
            parent_key = node.get("parent_key")
            parent = self.folder_items_by_key.get(parent_key) if parent_key else None
            if parent_key and parent is None:
                self.pending_folder_nodes.setdefault(parent_key, []).append(node)
                return

            item = SortableTreeWidgetItem([
                node["display_name"],
                direct_text,
                recursive_text,
                included_text,
                excluded_text,
            ])
            item.folder = node["folder"]
            item.folder_key = folder_key
            item.folder_empty = folder_empty
            self.folder_items_by_key[folder_key] = item

            if parent is not None:
                parent.addChild(item)
                parent.setExpanded(True)
            else:
                self.tree.addTopLevelItem(item)
                item.setExpanded(True)

            for child_node in self.pending_folder_nodes.pop(folder_key, []):
                self.add_progressive_folder_node(child_node)
        else:
            item = existing
            item.setText(0, node["display_name"])
            item.setText(1, direct_text)
            item.setText(2, recursive_text)
            item.setText(3, included_text)
            item.setText(4, excluded_text)
            item.folder = node["folder"]
            item.folder_empty = folder_empty

        if node.get("child_count", 0) > 0 and not folder_empty:
            item.setExpanded(True)
        if count_known:
            self.folder_direct_counts[folder_key] = direct_count
            self.folder_count_cache[folder_key] = direct_count
        style_folder_name_cell(item, node["display_name"])

    def on_folder_tree_loaded(self, load_id, tree_data, state, elapsed):
        """Populate the Qt folder tree from worker-built data."""
        if load_id != self.tree_load_id:
            return
        log_event(f"Applying folder tree: load_id={load_id}, elapsed={elapsed:.1f}s")
        _thread, _worker, after_open = self.active_tree_loads.get(load_id, (None, None, False))
        self.finalize_folder_tree(tree_data, state)
        if not self.active_loads:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setVisible(False)
            self.progress_label.clear()
        if elapsed > AUTO_LIGHT_LOAD_SECONDS:
            self.enable_light_load_mode_automatically("folder tree load", elapsed)
        if self.reload_current_folder_after_tree:
            self.reload_current_folder_after_tree = False
            if self.current_folder_key:
                self.load_current_folder()
        if after_open:
            self.opening_file = False
            if self.light_load_mode:
                log_event("Skipping timeline index in low disk I/O mode")
                self.agenda_progress_label.setText("Agenda indexing is skipped in low disk I/O mode.")
            else:
                self.start_timeline_index()

    def finalize_folder_tree(self, tree_data, state):
        """Finalize a progressively loaded folder tree."""
        log_event("Finalizing PST/OST folder tree")
        sort_column = 2
        sort_order = Qt.DescendingOrder
        self.tree.setSortingEnabled(False)
        self.folder_direct_counts = dict(state.get("folder_direct_counts", {}))
        self.folder_count_cache = dict(state.get("folder_count_cache", self.folder_count_cache))

        max_count = 0
        selected_item = None

        def finish_node(node):
            """Ensure a folder exists, then update final counts recursively."""
            nonlocal max_count
            nonlocal selected_item
            folder_key = node["folder_key"]
            direct_count = node["direct_count"]
            recursive_count = node["recursive_count"]

            if folder_key not in self.folder_items_by_key:
                self.add_progressive_folder_node({
                    "folder": node["folder"],
                    "folder_key": folder_key,
                    "parent_key": folder_key.rsplit("/", 1)[0] if "/" in folder_key else None,
                    "display_name": node["display_name"],
                    "direct_count": direct_count,
                    "recursive_count": recursive_count,
                    "count_known": node.get("count_known", True),
                    "child_count": len(node["children"]),
                })

            it = self.folder_items_by_key.get(folder_key)
            if it is None:
                return

            count_known = node.get("count_known", True)
            folder_empty = count_known and recursive_count == 0
            included_count, excluded_count = self.folder_filter_counts(folder_key, direct_count)
            it.setText(0, node["display_name"])
            it.setText(1, str(direct_count) if count_known else "")
            it.setText(2, str(recursive_count) if count_known else "")
            it.setText(3, str(included_count) if count_known else "")
            it.setText(4, str(excluded_count) if count_known else "")
            it.folder = node["folder"]
            it.folder_empty = folder_empty
            if folder_key == self.current_folder_key:
                selected_item = it

            for child in node["children"]:
                finish_node(child)

            if count_known:
                max_count = max(max_count, direct_count, recursive_count)
            if count_known:
                it.setExpanded(recursive_count > 0)
            else:
                it.setExpanded(len(node["children"]) > 0)

        finish_node(tree_data)

        # Style count columns after recursion so each heat map has its final max.
        included_max = max((self.folder_filter_counts(key, count)[0] for key, count in self.folder_direct_counts.items()), default=0)
        excluded_max = max((self.folder_filter_counts(key, count)[1] for key, count in self.folder_direct_counts.items()), default=0)

        def apply_count_styles(item):
            """Apply heat-map styling to folder count columns."""
            style_folder_count_cell(item, 1, int(item.text(1) or 0), max_count)
            style_folder_count_cell(item, 2, int(item.text(2) or 0), max_count)
            style_folder_count_cell(item, 3, int(item.text(3) or 0), included_max)
            style_folder_count_cell(item, 4, int(item.text(4) or 0), excluded_max)
            for index in range(item.childCount()):
                apply_count_styles(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            apply_count_styles(self.tree.topLevelItem(index))

        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.resizeColumnToContents(3)
        self.tree.resizeColumnToContents(4)
        self.set_initial_tree_width()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        self.sort_folder_tree(sort_column, sort_order)
        log_event(
            f"Loaded PST/OST folder tree: folders={len(self.folder_direct_counts)}, "
            f"direct_messages={sum(self.folder_direct_counts.values())}"
        )

    def sort_folder_tree(self, column=2, order=Qt.DescendingOrder):
        """Sort the folder tree and every visible child branch."""
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(column, order)

        def sort_children(item):
            """Sort descendants under one folder item."""
            item.sortChildren(column, order)
            for index in range(item.childCount()):
                sort_children(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            sort_children(self.tree.topLevelItem(index))

    def load_tree(self):
        """Compatibility wrapper for callers that need a tree refresh."""
        self.start_folder_tree_load()

    def refresh_folder_filter_columns(self):
        """Refresh filter count columns without rebuilding the folder tree."""
        included_max = 0
        excluded_max = 0
        values_by_key = {}
        for folder_key, item in self.folder_items_by_key.items():
            try:
                direct_count = int(item.text(1) or self.folder_direct_counts.get(folder_key, 0) or 0)
            except Exception:
                direct_count = self.folder_direct_counts.get(folder_key, 0) or 0
            included_count, excluded_count = self.folder_filter_counts(folder_key, direct_count)
            values_by_key[folder_key] = (direct_count, included_count, excluded_count)
            included_max = max(included_max, included_count)
            excluded_max = max(excluded_max, excluded_count)

        for folder_key, item in self.folder_items_by_key.items():
            direct_count, included_count, excluded_count = values_by_key.get(folder_key, (0, 0, 0))
            item.setText(3, str(included_count))
            item.setText(4, str(excluded_count))
            style_folder_count_cell(item, 3, included_count, included_max)
            style_folder_count_cell(item, 4, excluded_count, excluded_max)

    def set_initial_tree_width(self):
        """Size the folder tree columns and splitter for initial display."""
        count_width = 58
        self.tree.setColumnWidth(1, count_width)
        self.tree.setColumnWidth(2, count_width)
        self.tree.setColumnWidth(3, 72)
        self.tree.setColumnWidth(4, 72)

        folder_width = min(max(self.tree.columnWidth(0), 180), 320)
        self.tree.setColumnWidth(0, folder_width)

        tree_width = folder_width + (count_width * 2) + 144 + 24
        right_width = max(self.width() - tree_width, 800)
        self.main_splitter.setSizes([tree_width, right_width])

    def folder_filter_counts(self, folder_key, direct_count):
        """Return included and excluded message counts for a folder filter."""
        query = self.text_filter
        pattern = self.active_search_pattern()
        if self.time_filter is None and not query:
            return direct_count, 0
        if query and pattern is not None and self.global_search_index_ready:
            rows = self.indexed_messages_by_folder.get(folder_key, [])
            included = sum(1 for row in rows if self.pff_row_matches_active_filters(row, pattern))
            return included, max(direct_count - included, 0)
        if query and pattern is None:
            return 0, direct_count
        if self.time_filter is not None:
            values = self.folder_timestamps.get(folder_key, [])
            start, end = self.time_filter
            included = sum(1 for value in values if start <= value < end)
            return included, max(direct_count - included, 0)
        return direct_count, 0

    def start_timeline_index(self):
        """Start background indexing for the mailbox timeline."""
        if not self.reader or self.light_load_mode:
            return
        self.timeline_load_id += 1
        load_id = self.timeline_load_id
        total_messages = sum(self.folder_direct_counts.values())
        log_event(f"Starting mailbox index worker: load_id={load_id}, total_messages={total_messages}")

        thread = QThread()
        worker = TimelineIndexWorker(self.reader.get_root_folder(), load_id, total_messages, self.contact_cache)
        worker.moveToThread(thread)
        self.timeline_thread = thread
        self.timeline_worker = worker
        self.active_timeline_loads[load_id] = (thread, worker)

        self.timeline_stack.setCurrentWidget(self.timeline_index_widget)
        self.mailbox_index_started_at = time.monotonic()
        self.mailbox_index_progress_samples.clear()
        self.timeline_index_progress.setValue(0)
        self.timeline_index_label.setText("Indexing mailbox...")
        self.agenda_progress_bar.setVisible(True)
        self.agenda_progress_bar.setValue(0)
        self.agenda_progress_label.setText("Indexing agenda via mailbox index...")

        thread.started.connect(worker.run)
        worker.progress.connect(self.on_timeline_progress)
        worker.finished.connect(self.on_timeline_indexed)
        worker.finished.connect(lambda _load_id, _values, _folders, _rows, _agenda, thread=thread: thread.quit())
        worker.finished.connect(lambda _load_id, _values, _folders, _rows, _agenda, worker=worker: worker.deleteLater())
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda load_id=load_id: self.active_timeline_loads.pop(load_id, None))
        thread.start()

    def on_timeline_progress(self, load_id, cur, total):
        """Update timeline indexing progress for the active load."""
        if load_id != self.timeline_load_id:
            return
        if total:
            progress = int(cur / total * 100)
            now = time.monotonic()
            elapsed = max(now - (self.mailbox_index_started_at or now), 0.001)
            self.mailbox_index_progress_samples.append((now, cur))
            cutoff = now - 10.0
            while len(self.mailbox_index_progress_samples) > 1 and self.mailbox_index_progress_samples[0][0] < cutoff:
                self.mailbox_index_progress_samples.popleft()
            if len(self.mailbox_index_progress_samples) >= 2:
                sample_start, sample_cur = self.mailbox_index_progress_samples[0]
                sample_elapsed = max(now - sample_start, 0.001)
                hz = max(0, cur - sample_cur) / sample_elapsed
            else:
                hz = cur / elapsed if cur else 0
            remaining_seconds = ((total - cur) / hz) if hz > 0 else 0
            eta = datetime.now() + timedelta(seconds=remaining_seconds)
            status = (
                f"Indexing mailbox {cur}/{total} @ {hz:.0f} Hz - "
                f"ETA : {eta:%Y-%m-%d %H:%M:%S} - "
                f"Remaining : {format_duration(remaining_seconds)}"
            )
            self.timeline_index_progress.setValue(progress)
            self.timeline_index_label.setText(status)
            self.agenda_progress_bar.setValue(progress)
            self.agenda_progress_label.setText(status)

    def on_timeline_indexed(self, load_id, timeline_values, folder_timestamps, indexed_messages_by_folder, agenda_items):
        """Apply completed timeline index data to the UI."""
        if load_id != self.timeline_load_id:
            return
        indexed_count = sum(len(rows) for rows in indexed_messages_by_folder.values())
        log_event(
            f"Mailbox indexing finished: load_id={load_id}, "
            f"timestamps={len(timeline_values)}, folders={len(folder_timestamps)}, "
            f"indexed_messages={indexed_count}, agenda_items={len(agenda_items)}"
        )
        self.folder_timestamps = folder_timestamps
        self.indexed_messages_by_folder = indexed_messages_by_folder
        self.indexed_all_messages = [
            row for rows in indexed_messages_by_folder.values() for row in rows
        ]
        self.loaded_folder_messages_by_key.update(
            {folder_key: list(rows) for folder_key, rows in indexed_messages_by_folder.items()}
        )
        self.global_search_index_ready = True
        self.timeline_index_ready = True
        timeline_items = []
        for rows in indexed_messages_by_folder.values():
            for row in rows:
                parsed = message_effective_datetime(row)
                if parsed is not None:
                    timeline_items.append((parsed, folder_leaf(row.folder_name)))
        self.timeline.set_timeline_items(timeline_items)
        self.timeline_stack.setCurrentWidget(self.timeline)
        if self.current_folder_key:
            item = self.folder_items_by_key.get(self.current_folder_key)
            if item is not None:
                self.tree.setCurrentItem(item)
            self.update_timeline_overlay(self.model.messages)
        self.refresh_folder_filter_columns()
        self.apply_indexed_agenda_items(agenda_items)
        if self.text_filter:
            self.apply_search()

    def apply_indexed_agenda_items(self, items):
        """Apply agenda items produced by the combined mailbox index."""
        if self.light_load_mode:
            return
        self.agenda_model.set(items)
        self.apply_agenda_filter()
        self.agenda_progress_bar.setVisible(False)
        self.agenda_progress_label.setText(f"Loaded {len(items)} agenda items")
        self.update_agenda_columns()
        if self.agenda_model.rowCount():
            self.agenda_table.setCurrentIndex(self.agenda_model.index(0, 0))
        else:
            self.set_table_rows(self.agenda_detail_table, [])

    def apply_agenda_filter(self):
        """Apply agenda text search."""
        if not hasattr(self, "agenda_model"):
            return
        self.agenda_text_filter = self.agenda_search.text().lower().strip()
        if not self.agenda_text_filter:
            self.agenda_model.set_visible(self.agenda_model.all_items)
        else:
            self.agenda_model.set_visible([
                item for item in self.agenda_model.all_items
                if self.agenda_text_filter in self.agenda_model.searchable_text(item)
            ])
        self.agenda_calendar.set_items(self.agenda_model.items)
        self.update_agenda_columns()

    def on_agenda_select(self, idx):
        """Populate agenda details for the selected row."""
        item = self.agenda_model.item(idx.row())
        if not item:
            self.set_table_rows(self.agenda_detail_table, [])
            return
        self.agenda_calendar.set_selected_item(item)
        rows = [
            ("Start", agenda_datetime_text(item.start)),
            ("End", agenda_datetime_text(item.end)),
            ("Status", item.status),
            ("Subject", item.subject),
            ("Organizer", item.organizer),
            ("Attendees", item.attendees),
            ("Type", item.item_type),
            ("Folder", item.folder),
            ("Source", item.source),
        ]
        details = item.details or {}
        for key in sorted(details):
            rows.append((key, details[key]))
        self.set_table_rows(self.agenda_detail_table, rows)

    def select_agenda_item(self, item):
        """Select an agenda table row from the calendar view."""
        if item is None:
            return
        for row, row_item in enumerate(self.agenda_model.items):
            if row_item is item:
                self.agenda_table.setCurrentIndex(self.agenda_model.index(row, 0))
                self.agenda_table.setFocus()
                return

    def update_agenda_columns(self):
        """Size agenda table columns."""
        if not hasattr(self, "agenda_table"):
            return
        self.agenda_table.setColumnWidth(AgendaModel.START_COLUMN, 145)
        self.agenda_table.setColumnWidth(AgendaModel.END_COLUMN, 145)
        self.agenda_table.setColumnWidth(AgendaModel.STATUS_COLUMN, 120)
        self.agenda_table.setColumnWidth(AgendaModel.SUBJECT_COLUMN, 360)
        self.agenda_table.setColumnWidth(AgendaModel.ORGANIZER_COLUMN, 220)
        self.agenda_table.setColumnWidth(AgendaModel.ATTENDEES_COLUMN, 260)
        self.agenda_table.setColumnWidth(AgendaModel.TYPE_COLUMN, 140)
        self.agenda_table.setColumnWidth(AgendaModel.FOLDER_COLUMN, 220)
        self.agenda_table.setColumnWidth(AgendaModel.SOURCE_COLUMN, 220)

    def rendered_html_for_snapshot(self, snapshot):
        """Return the rendered HTML used by the preview pane for a snapshot."""
        html = renderable_html(snapshot.html)
        rtf_text = rtf_to_text(snapshot.rtf) if snapshot.rtf else ""
        rtf_html = rtf_to_html(snapshot.rtf) if snapshot.rtf else ""
        sections = []
        if not html:
            sections.append(render_section_html(
                "HTML content",
                "",
                "Warning: no HTML body was found for this message.",
            ))
        rtf_warning = None
        if not snapshot.rtf:
            rtf_warning = "Warning: no RTF body was found for this message."
        elif rtf_body_unparsable(snapshot.rtf, rtf_text):
            rtf_warning = "Warning: an RTF body was found but could not be parsed."
        elif not rtf_html and rtf_text:
            rtf_html = plain_body_html(rtf_text)
            rtf_warning = "Warning: RTF rich formatting could not be converted; showing parsed text."
        elif not rtf_html:
            rtf_warning = "Warning: an RTF body was found but no displayable content was extracted."
        sections.append(render_section_html("RTF content", rtf_html, rtf_warning))
        images_html = attachment_images_html(snapshot.attachments)
        if images_html:
            sections.append(images_html)
        if snapshot.text:
            sections.append(render_section_html("Text content", plain_body_html(snapshot.text)))
        tail_html = render_section_break().join(sections)
        if html:
            html = inline_cid_images(html, snapshot.attachments)
            html = prepend_html_fragment(html, render_section_html("HTML content", ""))
            return append_html_fragment(html, render_section_break() + tail_html if tail_html else "")
        if tail_html:
            return "<!doctype html><html><body>" + tail_html + "</body></html>"
        if snapshot.html:
            raw_html_section = render_section_html(
                "HTML source",
                plain_body_html(snapshot.html),
                "Warning: HTML-like source was found but could not be rendered as HTML.",
            )
            return "<!doctype html><html><body>" + raw_html_section + "</body></html>"
        return (
            "<!doctype html><html><body>"
            + render_section_html("Message body", "<p>No message body found.</p>")
            + "</body></html>"
        )

    def export_selected_messages(self):
        """Export checked PFF messages into a reporting zip."""
        messages = self.messages_selected_for_export()
        if not messages:
            QMessageBox.information(self, "Export emails", "Check or highlight one or more emails to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export selected emails",
            "selected-emails.zip",
            "Zip archives (*.zip)",
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"

        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, row_msg in enumerate(messages, 1):
                    snapshot = message_snapshot(raw_pff_message(row_msg), self.contact_cache)
                    folder = export_folder_name(index, snapshot.subject, snapshot.date)
                    smtp_headers = snapshot.raw_headers or "\n".join(
                        f"{name}: {value}" for name, value in snapshot.header_rows if s(value)
                    )
                    archive.writestr(f"{folder}/smtp-headers.txt", smtp_headers)
                    description = email_description_from_snapshot(snapshot)
                    description["folder"] = s(getattr(row_msg, "folder_name", ""))
                    description["folder_key"] = s(getattr(row_msg, "folder_key", ""))
                    archive.writestr(
                        f"{folder}/message.json",
                        json.dumps(description, indent=2, ensure_ascii=False),
                    )
                    summary_rows = [
                        ("Folder", getattr(row_msg, "folder_name", "")),
                        ("Subject", snapshot.subject),
                        ("Contact", snapshot.contact),
                        ("From", snapshot.sender),
                        ("To", snapshot.to),
                        ("CC", snapshot.cc),
                        ("BCC", snapshot.bcc),
                        ("Classification", snapshot.classification),
                        ("Date", snapshot.date),
                        ("# Attachments", str(len(snapshot.attachments)) if snapshot.attachments else ""),
                    ]
                    archive.writestr(
                        f"{folder}/message.html",
                        exported_html_document(summary_rows, self.rendered_html_for_snapshot(snapshot)),
                    )

                    used_names = set()
                    missing = []
                    for attachment_index, info in enumerate(snapshot.attachments, 1):
                        data = attachment_bytes(info["_object"])
                        filename = safe_export_name(info.get("name"), f"attachment-{attachment_index}", 120)
                        if "." not in filename and s(info.get("type")).split("/", 1)[-1]:
                            ext = s(info.get("type")).split("/", 1)[-1].split(";", 1)[0]
                            filename = f"{filename}.{safe_export_name(ext, 'bin', 12)}"
                        base, ext = os.path.splitext(filename)
                        candidate = filename
                        suffix = 2
                        while candidate.lower() in used_names:
                            candidate = f"{base}-{suffix}{ext}"
                            suffix += 1
                        used_names.add(candidate.lower())
                        if data is None:
                            missing.append(candidate)
                            continue
                        archive.writestr(f"{folder}/attachments/{candidate}", data)
                    if missing:
                        archive.writestr(
                            f"{folder}/attachments/missing-attachments.txt",
                            "Attachment bytes were not exposed by the installed PST/OST bindings:\n"
                            + "\n".join(missing),
                        )
        except Exception as exc:
            log_error("Unable to export selected emails", s(exc))
            QMessageBox.critical(self, "Unable to export selected emails", s(exc))
            return

        QMessageBox.information(self, "Export emails", f"Exported {len(messages)} emails to:\n{path}")

    def show_selected_messages(self):
        """Show checked export messages remembered across searches and folders."""
        messages = self.model.selected_messages()
        if not messages:
            QMessageBox.information(self, "Show selected", "No checked emails are currently marked for export.")
            return
        self.model.set_visible(messages)
        self.update_optional_columns()
        self.update_timeline_overlay(self.model.messages)
        self.progress_label.setText(f"Showing {len(messages)} checked emails marked for export")
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))
            self.table.setFocus()

    def messages_selected_for_export(self):
        """Return checked messages plus highlighted table rows."""
        messages = []
        seen = set()
        for msg in self.model.selected_messages():
            key = self.model.selection_key(msg)
            if key not in seen:
                messages.append(msg)
                seen.add(key)
        for idx in self.table.selectionModel().selectedRows():
            msg = self.model.msg(idx.row())
            key = self.model.selection_key(msg)
            if msg is not None and key not in seen:
                messages.append(msg)
                seen.add(key)
        return messages

    # ---------------- FOLDER LOAD ----------------
    def load_folder(self, item):
        """Start loading messages for the selected folder."""
        folder = item.folder
        folder_key = item.folder_key
        folder_name_text = item.text(0)
        if folder_key in self.loaded_folder_messages_by_key:
            log_event(f"Using cached folder load: key={folder_key}, name={folder_name_text!r}")
            self.current_folder_key = folder_key
            self.current_folder_loaded_key = folder_key
            self.current_folder_name = folder_name_text
            self.current_folder_messages = list(self.loaded_folder_messages_by_key[folder_key])
            self.update_loaded_messages(self.current_folder_messages)
            if self.model.rowCount():
                self.table.setCurrentIndex(self.model.index(0, 0))
                self.table.setFocus()
            else:
                self.clear_detail_tabs()
            return
        self.current_folder_key = item.folder_key
        self.current_folder_loaded_key = None
        self.current_folder_messages = []
        self.current_folder_name = item.text(0)
        self.load_id += 1
        load_id = self.load_id
        log_event(
            f"Starting folder load: key={self.current_folder_key}, "
            f"name={self.current_folder_name!r}, load_id={load_id}"
        )

        thread = QThread()
        worker = LoadWorker(folder, load_id, self.contact_cache, self.current_folder_key, self.current_folder_name)
        worker.moveToThread(thread)
        self.thread = thread
        self.worker = worker
        self.active_loads[load_id] = (thread, worker)

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Loading...")
        self.model.set([])
        self.update_optional_columns()
        self.timeline.clear_overlay()
        self.clear_detail_tabs()

        thread.started.connect(worker.run)
        worker.progress.connect(self.on_progress)
        worker.partial.connect(self.on_partial_loaded)
        worker.finished.connect(self.on_loaded)
        worker.finished.connect(lambda _load_id, _msgs, thread=thread: thread.quit())
        worker.finished.connect(lambda _load_id, _msgs, worker=worker: worker.deleteLater())
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda load_id=load_id: self.active_loads.pop(load_id, None))

        thread.start()

    # ---------------- PROGRESS ----------------
    def on_progress(self, load_id, cur, total):
        """Update folder loading progress for the active load."""
        if load_id != self.load_id:
            return
        if total:
            self.progress_bar.setValue(int(cur / total * 100))
            self.progress_label.setText(f"Loading {cur}/{total}")

    def on_partial_loaded(self, load_id, msgs, cur, total):
        """Show an early batch of messages while a folder continues loading."""
        if load_id != self.load_id:
            return
        self.update_loaded_messages(msgs, incremental=True)
        self.progress_label.setText(f"Loading {cur}/{total} - showing {self.model.rowCount()} loaded")

    # ---------------- DONE ----------------
    def on_loaded(self, load_id, msgs):
        """Apply the completed folder load to the message table."""
        if load_id != self.load_id:
            return
        self.update_loaded_messages(msgs)
        self.current_folder_loaded_key = self.current_folder_key
        if self.current_folder_key:
            self.loaded_folder_messages_by_key[self.current_folder_key] = list(msgs)
        if self.current_folder_key:
            item = self.folder_items_by_key.get(self.current_folder_key)
            existing_count = None
            if item is not None:
                try:
                    existing_count = int(item.text(1) or 0)
                except Exception:
                    existing_count = None
            if len(msgs) == 0 and existing_count and existing_count > 0:
                log_event(
                    f"Keeping existing folder count for {self.current_folder_key} "
                    f"({self.current_folder_name!r}): "
                    f"loaded 0 but tree shows {existing_count}"
                )
            else:
                self.folder_count_cache[self.current_folder_key] = len(msgs)
                self.folder_direct_counts[self.current_folder_key] = len(msgs)
                if item is not None:
                    included_count, excluded_count = self.folder_filter_counts(self.current_folder_key, len(msgs))
                    item.setText(1, str(len(msgs)))
                    if not item.text(2) or item.text(2) == "0":
                        item.setText(2, str(len(msgs)))
                    item.setText(3, str(included_count))
                    item.setText(4, str(excluded_count))
                    item.folder_empty = len(msgs) == 0
                    self.sort_folder_tree()
        self.progress_bar.setVisible(False)
        self.progress_label.clear()
        log_event(
            f"Folder load finished: key={self.current_folder_key}, "
            f"name={self.current_folder_name!r}, "
            f"messages={len(msgs)}, visible={self.model.rowCount()}"
        )
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))
            self.table.setFocus()

    def update_loaded_messages(self, msgs, incremental=False):
        """Refresh the message model and timeline overlay from loaded messages."""
        self.current_folder_messages = list(msgs)
        visible_msgs = self.filter_messages_for_time(msgs)
        if self.search.text().strip():
            self.model.all_messages = visible_msgs
            self.apply_search()
        else:
            if incremental:
                self.model.refresh_loaded(visible_msgs, visible_msgs)
            else:
                self.model.set(visible_msgs)
            self.update_optional_columns()
            self.update_timeline_overlay(self.model.messages)

    def filter_messages_for_time(self, msgs):
        """Return loaded messages inside the active time window."""
        if self.time_filter is None:
            return msgs
        start, end = self.time_filter
        visible = []
        for msg in msgs:
            parsed = message_effective_datetime(msg)
            if parsed is not None and start <= parsed < end:
                visible.append(msg)
        return visible

    def active_search_pattern(self):
        """Return compiled regex for the active search text, or None when invalid/empty."""
        query = self.text_filter.strip()
        if not query:
            return None
        try:
            return re.compile(query, re.IGNORECASE)
        except re.error:
            return None

    def pff_row_matches_active_filters(self, row, pattern=None):
        """Return whether an indexed PFF row matches active time and regex filters."""
        if self.time_filter is not None and not self.text_filter:
            start, end = self.time_filter
            parsed = message_effective_datetime(row)
            if parsed is None or not (start <= parsed < end):
                return False
        if self.text_filter:
            if pattern is None:
                return False
            if not pattern.search(row.search_text or pff_row_search_text(row)):
                return False
        return True

    def message_matches_active_search(self, msg, pattern=None):
        """Return whether a loaded message matches the active regex search."""
        if not self.text_filter:
            return True
        if pattern is None:
            return False
        return bool(pattern.search(self.model.searchable_text(msg)))

    def filtered_indexed_messages(self):
        """Return globally indexed rows matching active filters."""
        pattern = self.active_search_pattern()
        return [
            row for row in self.indexed_all_messages
            if self.pff_row_matches_active_filters(row, pattern)
        ]

    def update_timeline_overlay(self, msgs):
        """Update timeline overlay."""
        if self.light_load_mode:
            return
        values = []
        for msg in msgs:
            parsed = message_effective_datetime(msg)
            if parsed is not None:
                values.append(parsed)
        if values:
            folder_label = ""
            item = self.folder_items_by_key.get(self.current_folder_key)
            if item is not None:
                folder_label = item.text(0)
            self.timeline.set_overlay_timestamps(values, folder_label)
        else:
            self.timeline.clear_overlay()

    def apply_time_filter(self, start, end):
        """Apply time filter."""
        if self.light_load_mode:
            self.time_filter = None
            return
        self.time_filter = (start, end) if start is not None and end is not None else None
        self.refresh_folder_filter_columns()
        if self.text_filter and self.global_search_index_ready:
            self.apply_search()
        elif self.current_folder_key and self.current_folder_messages:
            self.update_loaded_messages(self.current_folder_messages)
        elif self.current_folder_key:
            self.timeline.clear_overlay()
        else:
            self.timeline.clear_overlay()

    def clear_time_filter(self):
        """Clear time filter."""
        self.timeline.clear_time_filter()

    def load_current_folder(self):
        """Load current folder."""
        item = self.folder_items_by_key.get(self.current_folder_key)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.load_folder(item)

    # ---------------- EMAIL SELECT ----------------
    def on_select(self, idx):
        """Handle select."""
        msg = self.model.msg(idx.row())
        if not msg:
            return

        snapshot = message_snapshot(raw_pff_message(msg), self.contact_cache)
        self.update_detail_tabs(snapshot, msg)
        body_html = self.rendered_html_for_snapshot(snapshot)
        self.set_html_source(body_html)
        self.body.setHtml(body_html)

    # ---------------- SORT ----------------
    def toggle_date_sort(self, checked):
        """Toggle the message table between newest-first and oldest-first sorting."""
        order = Qt.DescendingOrder if checked else Qt.AscendingOrder
        self.table.sortByColumn(MsgModel.DATE_COLUMN, order)
        self.date_sort_button.setText("Date: Newest" if checked else "Date: Oldest")
        self.update_optional_columns()
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))

    # ---------------- SEARCH ----------------
    def schedule_search(self):
        """Debounce text search after the query changes."""
        self.search_timer.start()

    def apply_search(self):
        """Apply the current search text to the loaded messages."""
        self.text_filter = self.search.text().strip()
        q = self.text_filter
        pattern = self.active_search_pattern()

        if q and pattern is None:
            self.model.set_visible([])
            self.update_optional_columns()
            self.timeline.clear_overlay()
            self.refresh_folder_filter_columns()
            self.progress_label.setText("Invalid regex search pattern")
            return

        if q and self.global_search_index_ready:
            matches = self.filtered_indexed_messages()
            self.model.set_visible(matches)
            self.update_optional_columns()
            self.update_timeline_overlay(self.model.messages)
            self.refresh_folder_filter_columns()
            self.progress_label.setText(f"Found {len(matches)} messages across mailbox subject, recipients, and body")
            return

        if not q:
            self.model.set_visible(self.model.all_messages)
            self.update_optional_columns()
            self.update_timeline_overlay(self.model.messages)
            self.refresh_folder_filter_columns()
            self.progress_label.clear()
            return

        self.model.set_visible([
            m for m in self.model.all_messages
            if self.message_matches_active_search(m, pattern)
        ])
        self.update_optional_columns()
        self.update_timeline_overlay(self.model.messages)
        self.refresh_folder_filter_columns()
        if self.global_search_index_ready:
            self.progress_label.setText(f"Found {self.model.rowCount()} messages")
        else:
            self.progress_label.setText(
                f"Found {self.model.rowCount()} messages in loaded folder; mailbox index is still building"
            )

    def update_optional_columns(self):
        """Update optional columns."""
        if not hasattr(self, "table"):
            return
        self.table.setColumnHidden(MsgModel.CONTACT_COLUMN, not self.model.has_contacts())
        self.table.setColumnHidden(MsgModel.BCC_COLUMN, not self.model.has_bcc())
        self.table.setColumnWidth(MsgModel.EXPORT_COLUMN, 58)
        self.table.setColumnWidth(MsgModel.TYPE_COLUMN, 44)
        self.table.setColumnWidth(MsgModel.SUBJECT_COLUMN, 320)
        self.table.setColumnWidth(MsgModel.FOLDER_COLUMN, 220)
        self.table.setColumnWidth(MsgModel.CONTACT_COLUMN, 180)
        self.table.setColumnWidth(5, 180)
        self.table.setColumnWidth(6, 220)
        self.table.setColumnWidth(7, 180)
        self.table.setColumnWidth(MsgModel.CLASSIFICATION_COLUMN, 150)
        self.table.setColumnWidth(MsgModel.DATE_COLUMN, 160)
        self.table.setColumnWidth(MsgModel.ATTACHMENT_COLUMN, 96)
        self.table.setColumnWidth(MsgModel.EMAIL_SIZE_COLUMN, 96)


# =========================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Browse PST/OST/PFF mailboxes.")
    parser.add_argument("path", nargs="?", help="Mailbox path to open.")
    parser.add_argument(
        "--light-load",
        "--low-io",
        action="store_true",
        dest="light_load_mode",
        help="Skip global timeline and contact-cache scans; load messages only when folders are selected.",
    )
    args = parser.parse_args()

    suppress_qt_font_warnings()
    app = QApplication([sys.argv[0]])
    apply_qt_app_icon(app)

    w = OSTViewer(initial_file=args.path, light_load_mode=args.light_load_mode)
    w.show()

    sys.exit(app.exec())
