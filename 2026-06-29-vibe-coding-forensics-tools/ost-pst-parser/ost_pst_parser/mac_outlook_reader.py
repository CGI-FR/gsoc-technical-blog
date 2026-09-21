#!/usr/bin/env python3

import json
import html as html_lib
import os
import re
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime

from .ui_helpers import (
    apply_qt_app_icon,
    log_error,
    log_event,
    normalize_native_path,
    pause_for_console,
    suppress_qt_font_warnings,
)

try:
    from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer
    from PySide6.QtGui import QColor, QBrush, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QFileDialog,
        QHeaderView,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSplitter,
        QTabWidget,
        QTableView,
        QTableWidget,
        QTableWidgetItem,
        QTextBrowser,
        QTextEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        message = (
            "Missing Qt dependency: PySide6\n\n"
            "Install the required GUI package with:\n"
            "  python -m pip install PySide6"
        )
        log_error("Missing Qt dependency", message)
        pause_for_console()
        raise SystemExit(1) from exc
    raise

from .email_domains import contains_external_email, contains_personal_email, looks_like_email
from .agenda_view import AgendaItem, AgendaModel, AgendaTwoWeekView, agenda_datetime_text
from .formatting import format_bool, format_bytes, s, short_display
from .table_colors import (
    attachment_brush,
    dark_text_brush,
    external_address_brush,
    heat_brushes,
    personal_address_brush,
)
from .timeline_view import TimelineBarChart, folder_color, normalize_message_datetime


def format_timestamp(value):
    """Format timestamp."""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except Exception:
        return s(value)

    # Outlook for Mac stores these message times as Unix timestamps.
    try:
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s(value)


def calendar_datetime(value):
    """Return a datetime for Outlook Mac calendar timestamps."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return normalize_message_datetime(value)
    if number <= 0 or number > 4102444800:
        return None
    if number < 946684800:
        number += 978307200
    return normalize_message_datetime(number)


def format_calendar_timestamp(value):
    """Format an Outlook Mac calendar timestamp."""
    parsed = calendar_datetime(value)
    if parsed is not None:
        return agenda_datetime_text(parsed)
    return format_timestamp(value)


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


MAIL_SEARCH_FIELDS = (
    "Message_NormalizedSubject",
    "Message_ThreadTopic",
    "Message_SenderList",
    "Message_SenderAddressList",
    "Message_RecipientList",
    "Message_DisplayTo",
    "Message_ToRecipientAddressList",
    "Message_CCRecipientAddressList",
    "Message_Preview",
    "Message_MessageID",
    "Record_ExchangeOrEasId",
    "PathToDataFile",
    "Categories",
)


def mail_search_text(values):
    """Return mail search text."""
    return "\n".join(s(values.get(field)) for field in MAIL_SEARCH_FIELDS).lower()


def mail_matches_text(values, query):
    """Return mail matches text."""
    query = s(query).lower().strip()
    return not query or query in mail_search_text(values)


def account_record_id_from_uid(value):
    """Return account record id from uid."""
    try:
        number = int(value or 0)
    except Exception:
        return None
    if number <= 0:
        return None
    return number & 0xFFFFFFFF


def date_sort_key(value):
    """Return date sort key."""
    if value in (None, ""):
        return (0, "")
    try:
        return (2, float(value))
    except Exception:
        return (1, s(value))


def folder_display_name(folder):
    """Return folder display name."""
    if s(folder.account_display_name).strip():
        return s(folder.account_display_name).strip()
    name = s(folder.name).strip()
    if name:
        return name
    if folder.parent_id is None or folder.parent_id < 0:
        return f"Account {folder.account_uid}"
    return "(unnamed folder)"


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
            fmt = self.key_format if re.match(r"\s*:", text[match.end():]) else self.string_format
            self.setFormat(match.start(), match.end() - match.start(), fmt)


@dataclass
class FolderRow:
    record_id: int
    parent_id: int | None
    name: str
    path: str
    account_uid: int
    folder_class: int
    folder_type: int
    folder_order: int
    account_display_name: str = ""
    direct_count: int = 0
    total_count: int = 0
    included_count: int = 0
    excluded_count: int = 0


@dataclass
class MailRow:
    values: dict
    categories: list

    def get(self, key, default=""):
        """Return a stored field value by key."""
        return self.values.get(key, default)

    @property
    def record_id(self):
        """Return record id."""
        return self.get("Record_RecordID")

    @property
    def subject(self):
        """Return the message subject text."""
        return self.get("Message_NormalizedSubject") or self.get("Message_ThreadTopic") or "(no subject)"

    @property
    def sender(self):
        """Return the message sender text."""
        return self.get("Message_SenderAddressList") or self.get("Message_SenderList")

    @property
    def to(self):
        """Return the message's To recipients."""
        return self.get("Message_ToRecipientAddressList") or self.get("Message_DisplayTo")

    @property
    def cc(self):
        """Return the message's Cc recipients."""
        return self.get("Message_CCRecipientAddressList")

    @property
    def date_value(self):
        """Return date value."""
        return self.get("Message_TimeReceived") or self.get("Message_TimeSent") or self.get("Record_ModDate")

    @property
    def date_text(self):
        """Return date text."""
        return format_timestamp(self.date_value)

    @property
    def effective_datetime(self):
        """Return the best available datetime for sorting and filtering."""
        for key in ("Message_TimeReceived", "Message_TimeSent", "Record_ModDate"):
            parsed = normalize_message_datetime(self.get(key))
            if parsed is not None:
                return parsed
        return None

    @property
    def path(self):
        """Return the message data-file path."""
        return self.get("PathToDataFile")

    @property
    def preview(self):
        """Return the message preview text."""
        return self.get("Message_Preview")

    @property
    def category_names(self):
        """Return category names attached to the message."""
        return ", ".join(s(category.get("Category_Name")) for category in self.categories if s(category.get("Category_Name")))


class OutlookSqliteStore:
    MAIL_COLUMNS = [
        "Record_RecordID",
        "PathToDataFile",
        "Record_ModDate",
        "Record_FolderID",
        "Record_AccountUID",
        "Message_type",
        "Message_HasAttachment",
        "Message_Hidden",
        "Message_ImapUID",
        "Message_IsOutgoingMessage",
        "Message_MarkedForDelete",
        "Message_MentionedMe",
        "Message_MessageID",
        "Message_NormalizedSubject",
        "Message_PartiallyDownloaded",
        "Message_DownloadState",
        "Message_ReadFlag",
        "Message_RecipientList",
        "Message_DisplayTo",
        "Message_Preview",
        "Message_SenderList",
        "Message_Sent",
        "Message_Size",
        "Message_Status",
        "Conversation_ConversationID",
        "Message_ThreadTopic",
        "Message_TimeReceived",
        "Message_TimeSent",
        "Record_ExchangeOrEasId",
        "Record_ExchangeChangeKey",
        "Record_FlagStatus",
        "Record_Priority",
        "Record_HasReminder",
        "Record_Recover",
        "Message_InferenceClassification",
        "Threads_ThreadID",
        "Record_DedupeId",
        "Message_ToRecipientAddressList",
        "Message_CCRecipientAddressList",
        "Message_SenderAddressList",
        "Message_ItemMigrationStatus",
    ]

    def __init__(self, path):
        """Initialize the instance and its default state."""
        self.path = path
        log_event(f"Opening Outlook SQLite database: {path}")
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma query_only = on")
        self.validate_schema()
        self.account_names = self.load_account_names()
        self.primary_account_name = self.pick_primary_account_name()
        log_event(
            f"Opened Outlook SQLite database: {path}, "
            f"accounts={len(self.account_names)}, categories={self.has_categories}"
        )

    def close(self):
        """Close the underlying database connection."""
        if self.conn:
            log_event(f"Closing Outlook SQLite database: {self.path}")
            self.conn.close()
            self.conn = None

    def validate_schema(self):
        """Validate that the database contains the required Outlook tables."""
        log_event("Validating Outlook SQLite schema")
        tables = {
            row["name"]
            for row in self.conn.execute(
                """
                select name
                from sqlite_master
                where type='table'
                  and name in (
                    'Mail',
                    'Folders',
                    'Categories',
                    'Mail_Categories',
                    'CalendarEvents',
                    'CalendarEvents_Categories'
                  )
                """
            )
        }
        missing = {"Mail", "Folders"} - tables
        if missing:
            raise ValueError(f"Missing required table(s): {', '.join(sorted(missing))}")
        self.has_categories = {"Categories", "Mail_Categories"}.issubset(tables)
        self.has_calendar_events = "CalendarEvents" in tables
        self.has_calendar_categories = {"Categories", "CalendarEvents_Categories"}.issubset(tables)
        log_event(f"Validated Outlook SQLite schema: tables={', '.join(sorted(tables))}")

    def load_account_names(self):
        """Load account names."""
        account_names = {}
        tables = [
            row["name"]
            for row in self.conn.execute(
                "select name from sqlite_master where type='table' and name like 'Accounts%' order by name"
            )
        ]
        for table in tables:
            columns = [row["name"] for row in self.conn.execute(f"pragma table_info({table})")]
            if "Record_RecordID" not in columns or "Account_Name" not in columns:
                continue

            select_columns = ["Record_RecordID", "Account_Name"]
            has_email_address = "Account_EmailAddress" in columns
            if has_email_address:
                select_columns.append("Account_EmailAddress")

            rows = self.conn.execute(f"select {', '.join(select_columns)} from {table}")
            for row in rows:
                account_id = row["Record_RecordID"]
                account_name = s(row["Account_Name"]).strip()
                email_address = s(row["Account_EmailAddress"]).strip() if has_email_address else ""
                display_name = account_name if looks_like_email(account_name) else email_address or account_name
                if display_name and account_id not in account_names:
                    account_names[account_id] = display_name
        return account_names

    def pick_primary_account_name(self):
        """Choose primary account name."""
        for value in self.account_names.values():
            if looks_like_email(value):
                return value
        return next(iter(self.account_names.values()), "")

    def load_folders(self, time_filter=None, text_filter=""):
        """Load Mac Outlook folders and compute filter-aware counts."""
        log_event(
            "Loading SQLite folders"
            + (f" with text_filter={text_filter!r}" if text_filter else "")
            + (" with time_filter" if time_filter is not None else "")
        )
        folders = {}
        # Load raw folder rows first so relationships can be built after account
        # display names and direct message counts are known.
        rows = self.conn.execute(
            """
            select
                Record_RecordID,
                Folder_ParentID,
                Folder_Name,
                PathToDataFile,
                Record_AccountUID,
                Folder_FolderClass,
                Folder_FolderType,
                Folder_FolderOrder
            from Folders
            order by Record_AccountUID, Folder_ParentID, Folder_FolderOrder, Folder_Name
            """
        )
        for row in rows:
            folder = FolderRow(
                record_id=row["Record_RecordID"],
                parent_id=row["Folder_ParentID"],
                name=s(row["Folder_Name"]),
                path=s(row["PathToDataFile"]),
                account_uid=row["Record_AccountUID"] or 0,
                folder_class=row["Folder_FolderClass"] or 0,
                folder_type=row["Folder_FolderType"] or 0,
                folder_order=row["Folder_FolderOrder"] or 0,
            )
            folders[folder.record_id] = folder

        for folder in folders.values():
            if folder.parent_id in folders:
                continue
            folder_name = s(folder.name).strip()
            if folder_name and not folder_name.startswith("Placeholder_"):
                continue
            account_id = account_record_id_from_uid(folder.account_uid)
            account_name = self.account_names.get(account_id, "")
            if not account_name and not s(folder.name).strip() and folder.account_uid:
                account_name = self.primary_account_name
            folder.account_display_name = account_name

        counts = self.conn.execute(
            """
            select Record_FolderID, count(*) as count
            from Mail
            group by Record_FolderID
            """
        )
        for row in counts:
            folder = folders.get(row["Record_FolderID"])
            if folder:
                folder.direct_count = row["count"]

        # When filters are active, scan mail rows once and count messages that
        # pass both the time window and searchable text fields.
        text_filter = s(text_filter).lower().strip()
        if time_filter is None and not text_filter:
            for folder in folders.values():
                folder.included_count = folder.direct_count
                folder.excluded_count = 0
        else:
            start, end = time_filter if time_filter is not None else (None, None)
            category_names = self.load_category_names_by_mail() if self.has_categories and text_filter else {}
            for row in self.conn.execute(
                """
                select
                    Record_RecordID,
                    PathToDataFile,
                    Record_FolderID,
                    Message_MessageID,
                    Message_NormalizedSubject,
                    Message_RecipientList,
                    Message_DisplayTo,
                    Message_Preview,
                    Message_SenderList,
                    Message_ThreadTopic,
                    Message_TimeReceived,
                    Message_TimeSent,
                    Record_ModDate,
                    Record_ExchangeOrEasId,
                    Message_ToRecipientAddressList,
                    Message_CCRecipientAddressList,
                    Message_SenderAddressList
                from Mail
                """
            ):
                folder = folders.get(row["Record_FolderID"])
                if not folder:
                    continue
                values = dict(row)
                values["Categories"] = category_names.get(row["Record_RecordID"], "")
                parsed = None
                for column in ("Message_TimeReceived", "Message_TimeSent", "Record_ModDate"):
                    parsed = normalize_message_datetime(row[column])
                    if parsed is not None:
                        break
                matches_time = time_filter is None or (parsed is not None and start <= parsed < end)
                matches_text = mail_matches_text(values, text_filter)
                if matches_time and matches_text:
                    folder.included_count += 1
                else:
                    folder.excluded_count += 1

        children = {}
        for folder in folders.values():
            children.setdefault(folder.parent_id, []).append(folder)

        def total(folder):
            """Return the recursive message total for a folder."""
            folder.total_count = folder.direct_count + sum(total(child) for child in children.get(folder.record_id, []))
            return folder.total_count

        for folder in folders.values():
            if folder.parent_id not in folders:
                total(folder)

        log_event(
            f"Loaded SQLite folders: folders={len(folders)}, "
            f"direct_messages={sum(folder.direct_count for folder in folders.values())}"
        )
        return folders, children

    def load_category_names_by_mail(self, folder_id=None):
        """Load category display names keyed by Mac Outlook mail record ID."""
        if not self.has_categories:
            return {}

        where = ""
        params = ()
        if folder_id is not None:
            where = "where mc.Record_FolderID = ?"
            params = (folder_id,)

        rows = self.conn.execute(
            f"""
            select mc.Record_RecordID as Mail_RecordID, c.Category_Name
            from Mail_Categories mc
            join Categories c on c.Record_RecordID = mc.Category_RecordID
            {where}
            order by c.Category_Name
            """,
            params,
        )
        names_by_mail = {}
        for row in rows:
            name = s(row["Category_Name"]).strip()
            if name:
                names_by_mail.setdefault(row["Mail_RecordID"], []).append(name)
        return {mail_id: ", ".join(names) for mail_id, names in names_by_mail.items()}

    def load_messages(self, folder_id):
        """Load messages and category labels for a Mac Outlook folder."""
        log_event(f"Loading SQLite messages for folder_id={folder_id}")
        select_columns = ", ".join(self.MAIL_COLUMNS)
        rows = self.conn.execute(
            f"""
            select {select_columns}
            from Mail
            where Record_FolderID = ?
            order by Message_TimeReceived desc, Message_TimeSent desc, Record_ModDate desc
            """,
            (folder_id,),
        )
        messages = [MailRow(dict(row), []) for row in rows]
        if not self.has_categories or not messages:
            log_event(f"Loaded SQLite messages for folder_id={folder_id}: messages={len(messages)}")
            return messages

        category_rows = self.conn.execute(
            """
            select
                mc.Record_RecordID as Mail_RecordID,
                c.Record_RecordID as Category_RecordID,
                c.Category_Name,
                c.PathToDataFile,
                c.Record_AccountUID,
                c.Record_FolderID,
                c.Category_Exchange_IsLocalCategory,
                c.Cateogry_ExchangeGuid,
                hex(c.Category_BackgroundColor) as Category_BackgroundColorHex
            from Mail_Categories mc
            join Categories c on c.Record_RecordID = mc.Category_RecordID
            where mc.Record_FolderID = ?
            order by c.Category_Name
            """,
            (folder_id,),
        )
        categories_by_mail = {}
        for row in category_rows:
            category = dict(row)
            mail_id = category.pop("Mail_RecordID")
            categories_by_mail.setdefault(mail_id, []).append(category)

        for message in messages:
            message.categories = categories_by_mail.get(message.record_id, [])
            message.values["Categories"] = message.category_names
        log_event(f"Loaded SQLite messages for folder_id={folder_id}: messages={len(messages)}")
        return messages

    def load_timeline_timestamps(self):
        """Load timeline timestamps."""
        log_event("Starting SQLite timestamp parse")
        rows = self.conn.execute(
            """
            select Message_TimeReceived, Message_TimeSent, Record_ModDate
            from Mail
            """
        )
        timestamps = []
        for row in rows:
            for column in ("Message_TimeReceived", "Message_TimeSent", "Record_ModDate"):
                if normalize_message_datetime(row[column]) is not None:
                    timestamps.append(row[column])
                    break
        log_event(f"Finished SQLite timestamp parse: timestamps={len(timestamps)}")
        return timestamps

    def load_timeline_items(self):
        """Load timeline timestamps with folder labels for colored grouping."""
        log_event("Starting SQLite timeline item parse")
        rows = self.conn.execute(
            """
            select
                m.Message_TimeReceived,
                m.Message_TimeSent,
                m.Record_ModDate,
                coalesce(f.Folder_Name, '') as Folder_Name
            from Mail m
            left join Folders f on f.Record_RecordID = m.Record_FolderID
            """
        )
        items = []
        for row in rows:
            for column in ("Message_TimeReceived", "Message_TimeSent", "Record_ModDate"):
                parsed = normalize_message_datetime(row[column])
                if parsed is not None:
                    items.append((parsed, row["Folder_Name"] or "Mailbox"))
                    break
        log_event(f"Finished SQLite timeline item parse: timestamps={len(items)}")
        return items

    def load_folder_names_by_id(self):
        """Load folder display names keyed by folder record ID."""
        rows = self.conn.execute("select Record_RecordID, Folder_Name from Folders")
        return {row["Record_RecordID"]: s(row["Folder_Name"]).strip() for row in rows}

    def load_calendar_category_names_by_event(self):
        """Load category display names keyed by calendar event record ID."""
        if not self.has_calendar_categories:
            return {}
        rows = self.conn.execute(
            """
            select cc.Record_RecordID as Event_RecordID, c.Category_Name
            from CalendarEvents_Categories cc
            join Categories c on c.Record_RecordID = cc.Category_RecordID
            order by c.Category_Name
            """
        )
        names_by_event = {}
        for row in rows:
            name = s(row["Category_Name"]).strip()
            if name:
                names_by_event.setdefault(row["Event_RecordID"], []).append(name)
        return {event_id: ", ".join(names) for event_id, names in names_by_event.items()}

    def load_agenda_items(self):
        """Load calendar events and meeting-related mail rows for the agenda view."""
        log_event("Loading SQLite agenda items")
        items = []
        folder_names = self.load_folder_names_by_id()

        if self.has_calendar_events:
            calendar_categories = self.load_calendar_category_names_by_event()
            rows = self.conn.execute(
                """
                select *
                from CalendarEvents
                order by Calendar_StartDateUTC, Calendar_EndDateUTC, Record_ModDate
                """
            )
            for row in rows:
                values = dict(row)
                record_id = values.get("Record_RecordID")
                folder = folder_names.get(values.get("Record_FolderID"), "")
                categories = calendar_categories.get(record_id, "")
                status_bits = []
                if values.get("Calendar_IsRecurring"):
                    status_bits.append("Recurring")
                if values.get("Record_Recover"):
                    status_bits.append("Recover")
                if categories:
                    status_bits.append(categories)
                subject = s(values.get("Calendar_UID")).strip() or f"Calendar event {record_id}"
                details = dict(values)
                details["Categories"] = categories
                details["Start"] = format_calendar_timestamp(values.get("Calendar_StartDateUTC"))
                details["End"] = format_calendar_timestamp(values.get("Calendar_EndDateUTC"))
                items.append(AgendaItem(
                    start=calendar_datetime(values.get("Calendar_StartDateUTC")),
                    end=calendar_datetime(values.get("Calendar_EndDateUTC")),
                    subject=subject,
                    attendees=s(values.get("Calendar_AttendeeCount")),
                    status=", ".join(status_bits),
                    item_type="Calendar Event",
                    folder=folder,
                    source="CalendarEvents",
                    source_ref=record_id,
                    details=details,
                ))

        select_columns = ", ".join(self.MAIL_COLUMNS)
        mail_rows = self.conn.execute(
            f"""
            select {select_columns}
            from Mail
            where
                lower(coalesce(Message_NormalizedSubject, '') || ' ' || coalesce(Message_ThreadTopic, '') || ' ' || coalesce(Message_Preview, ''))
                    glob '*meeting*'
                or lower(coalesce(Message_NormalizedSubject, '') || ' ' || coalesce(Message_ThreadTopic, '') || ' ' || coalesce(Message_Preview, ''))
                    glob '*appointment*'
                or lower(coalesce(Message_NormalizedSubject, '') || ' ' || coalesce(Message_ThreadTopic, '') || ' ' || coalesce(Message_Preview, ''))
                    glob '*declin*'
                or lower(coalesce(Message_NormalizedSubject, '') || ' ' || coalesce(Message_ThreadTopic, '') || ' ' || coalesce(Message_Preview, ''))
                    glob '*accept*'
                or lower(coalesce(Message_NormalizedSubject, '') || ' ' || coalesce(Message_ThreadTopic, '') || ' ' || coalesce(Message_Preview, ''))
                    glob '*tentative*'
            order by Message_TimeReceived, Message_TimeSent, Record_ModDate
            """
        )
        category_names = self.load_category_names_by_mail() if self.has_categories else {}
        for row in mail_rows:
            message = MailRow(dict(row), [])
            message.values["Categories"] = category_names.get(message.record_id, "")
            text = mail_search_text(message.values)
            status = ""
            if "declin" in text or "refus" in text:
                status = "Declined/Refused"
            elif "tentative" in text:
                status = "Tentative"
            elif "accept" in text:
                status = "Accepted"
            elif "cancel" in text:
                status = "Cancelled"
            folder = folder_names.get(message.get("Record_FolderID"), "")
            items.append(AgendaItem(
                start=message.effective_datetime,
                end=None,
                subject=message.subject,
                organizer=message.sender,
                attendees=message.to,
                status=status,
                item_type="Meeting Mail",
                folder=folder,
                source="Mail",
                source_ref=message,
                details=dict(message.values),
            ))

        log_event(f"Loaded SQLite agenda items: items={len(items)}")
        return items


class MsgModel(QAbstractTableModel):
    HEADERS = ["Export", "Subject", "From", "To", "CC", "Categories", "Date", "Attachment", "Read", "Size", "Path"]
    EXPORT_COLUMN = 0
    SUBJECT_COLUMN = 1
    ADDRESS_COLUMNS = {2, 3, 4}
    CC_COLUMN = 4
    CATEGORIES_COLUMN = 5
    DATE_COLUMN = 6
    ATTACHMENT_COLUMN = 7
    READ_COLUMN = 8
    EMAIL_SIZE_COLUMN = 9
    PATH_COLUMN = 10

    def __init__(self):
        """Initialize the instance and its default state."""
        super().__init__()
        self.all_messages = []
        self.messages = []
        self.sort_descending = True
        self.sort_column = self.DATE_COLUMN
        self.sort_order = Qt.DescendingOrder
        self.search_text_cache = {}
        self.visible_max_size = 0
        self.checked_message_ids = set()

    def set(self, messages):
        """Replace the model contents and refresh visible state."""
        self.beginResetModel()
        self.search_text_cache.clear()
        self.all_messages = messages
        self.messages = self.sorted_messages(messages)
        self.visible_max_size = self.max_message_size(self.messages)
        self.endResetModel()

    def set_visible(self, messages):
        """Set visible."""
        self.beginResetModel()
        self.messages = self.sorted_messages(messages)
        self.visible_max_size = self.max_message_size(self.messages)
        self.endResetModel()

    def set_sort_descending(self, descending):
        """Set sort descending."""
        if self.sort_descending == descending:
            return
        self.sort_descending = descending
        self.beginResetModel()
        self.messages = self.sorted_messages(self.messages)
        self.visible_max_size = self.max_message_size(self.messages)
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
        self.endResetModel()

    def message_size_value(self, msg):
        """Return message size value."""
        try:
            return int(msg.get("Message_Size") or 0)
        except Exception:
            return 0

    def max_message_size(self, messages):
        """Return the largest visible message size for heat-map scaling."""
        return max((self.message_size_value(msg) for msg in messages), default=0)

    def sorted_messages(self, messages):
        """Return messages sorted by the active column and direction."""
        reverse = self.sort_order == Qt.DescendingOrder
        indexed = list(enumerate(messages))
        indexed.sort(key=lambda item: self.sort_key(item[1], item[0]), reverse=reverse)
        return [msg for _index, msg in indexed]

    def sort_key(self, msg, original_index):
        """Return a stable, column-aware sort key for a message."""
        column = self.sort_column
        if column == self.EXPORT_COLUMN:
            return (self.selection_key(msg) in self.checked_message_ids, original_index)
        if column == self.SUBJECT_COLUMN:
            return (s(msg.subject).lower(), original_index)
        if column in self.ADDRESS_COLUMNS:
            return (self.address_value(msg, column).lower(), original_index)
        if column == self.CATEGORIES_COLUMN:
            return (msg.category_names.lower(), original_index)
        if column == self.DATE_COLUMN:
            return (*date_sort_key(msg.date_value), original_index)
        if column == self.ATTACHMENT_COLUMN:
            return (1 if bool(msg.get("Message_HasAttachment")) else 0, original_index)
        if column == self.READ_COLUMN:
            return (1 if bool(msg.get("Message_ReadFlag")) else 0, original_index)
        if column == self.EMAIL_SIZE_COLUMN:
            return (self.message_size_value(msg), original_index)
        if column == self.PATH_COLUMN:
            return (s(msg.path).lower(), original_index)
        return ("", original_index)

    def searchable_text(self, msg):
        """Return cached lowercase text used by message search."""
        cache_key = id(msg)
        if cache_key not in self.search_text_cache:
            self.search_text_cache[cache_key] = "\n".join(
                s(part)
                for part in (
                    msg.subject,
                    msg.sender,
                    msg.to,
                    msg.cc,
                    msg.category_names,
                    msg.date_text,
                    msg.path,
                    msg.preview,
                    msg.get("Message_MessageID"),
                    msg.get("Record_ExchangeOrEasId"),
                )
            ).lower()
        return self.search_text_cache[cache_key]

    def has_cc(self):
        """Return whether cc."""
        return any(s(msg.cc).strip() for msg in self.messages)

    def has_categories(self):
        """Return whether categories."""
        return any(s(msg.category_names).strip() for msg in self.messages)

    def rowCount(self, parent=QModelIndex()):
        """Return the number of table rows for Qt."""
        return len(self.messages)

    def columnCount(self, parent=QModelIndex()):
        """Return the number of table columns for Qt."""
        return len(self.HEADERS)

    def headerData(self, section, orientation, role):
        """Return header text for the Qt table model."""
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, idx, role):
        """Return display, sorting, and styling data for a table cell."""
        if not idx.isValid():
            return None

        msg = self.messages[idx.row()]

        if idx.column() == self.EXPORT_COLUMN:
            if role == Qt.CheckStateRole:
                return Qt.Checked if self.selection_key(msg) in self.checked_message_ids else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""

        if role == Qt.ToolTipRole and idx.column() == self.PATH_COLUMN:
            return s(msg.path)

        if idx.column() in self.ADDRESS_COLUMNS:
            address_value = self.address_value(msg, idx.column())
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

        if idx.column() == self.ATTACHMENT_COLUMN and bool(msg.get("Message_HasAttachment")):
            if role == Qt.BackgroundRole:
                return attachment_brush()
            if role == Qt.ForegroundRole:
                return dark_text_brush()

        if idx.column() == self.EMAIL_SIZE_COLUMN and self.visible_max_size > 0:
            if role == Qt.BackgroundRole:
                background, _foreground = heat_brushes(self.message_size_value(msg), self.visible_max_size)
                return background
            if role == Qt.ForegroundRole:
                return dark_text_brush()

        if role != Qt.DisplayRole:
            return None

        column = idx.column()
        if column == self.SUBJECT_COLUMN:
            return s(msg.subject)
        if column == 2:
            return s(msg.sender)
        if column == 3:
            return s(msg.to)
        if column == self.CC_COLUMN:
            return s(msg.cc)
        if column == self.CATEGORIES_COLUMN:
            return msg.category_names
        if column == self.DATE_COLUMN:
            return msg.date_text
        if column == self.ATTACHMENT_COLUMN:
            return format_bool(msg.get("Message_HasAttachment"))
        if column == self.READ_COLUMN:
            return format_bool(msg.get("Message_ReadFlag"))
        if column == self.EMAIL_SIZE_COLUMN:
            return format_bytes(self.message_size_value(msg))
        if column == self.PATH_COLUMN:
            return s(msg.path)
        return None

    def address_value(self, msg, column):
        """Return the address text for a message table address column."""
        if column == 2:
            return msg.sender
        if column == 3:
            return msg.to
        if column == self.CC_COLUMN:
            return msg.cc
        return ""

    def msg(self, row):
        """Return the backing message object for a row."""
        return self.messages[row] if 0 <= row < len(self.messages) else None

    def selection_key(self, msg):
        """Return stable selection key for a message."""
        return msg.record_id if msg is not None else None

    def selected_messages(self):
        """Return checked messages in loaded order."""
        return [
            msg for msg in self.all_messages
            if self.selection_key(msg) in self.checked_message_ids
        ]

    def clear_selection(self):
        """Clear checked export rows."""
        if not self.checked_message_ids:
            return
        self.checked_message_ids.clear()
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
        else:
            self.checked_message_ids.discard(key)
        self.dataChanged.emit(idx, idx)
        return True


class MacOutlookReader(QMainWindow):
    def __init__(self, initial_file=None):
        """Initialize the instance and its default state."""
        super().__init__()
        self.setWindowTitle("Mac Outlook SQLite Reader")
        apply_qt_app_icon(self)
        self.resize(1400, 850)

        self.store = None
        self.folders = {}
        self.folder_children = {}
        self.folder_items_by_id = {}
        self.opened_file_name = ""
        self.time_filter = None
        self.current_folder_id = None
        self.current_folder_loaded_id = None
        self.current_folder_messages = []
        self.loaded_folder_messages_by_id = {}
        self.text_filter = ""
        self.agenda_text_filter = ""

        self._ui()

        if initial_file:
            self.open_file(initial_file)

    def _ui(self):
        """Build and connect the window user interface."""
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        open_action = file_menu.addAction("Open Outlook SQLite")
        open_action.triggered.connect(self.open_dialog)
        filter_menu = menubar.addMenu("Filter")
        clear_time_filter_action = filter_menu.addAction("Clear Time Filter")
        clear_time_filter_action.triggered.connect(self.clear_time_filter)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Folders", "Items", "Total", "Included", "Excluded"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.setSortingEnabled(True)
        self.tree.itemClicked.connect(self.load_folder)

        self.model = MsgModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(MsgModel.DATE_COLUMN, Qt.DescendingOrder)
        self.table.selectionModel().currentChanged.connect(lambda current, previous: self.on_select(current))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.apply_text_filter)
        self.search.textChanged.connect(self.schedule_search)

        self.date_sort_button = QPushButton("Date: Newest")
        self.date_sort_button.setCheckable(True)
        self.date_sort_button.setChecked(True)
        self.date_sort_button.setToolTip("Toggle message date sort order")
        self.date_sort_button.clicked.connect(self.toggle_date_sort)
        self.export_button = QPushButton("Export")
        self.export_button.setToolTip("Export checked emails to a reporting zip")
        self.export_button.clicked.connect(self.export_selected_messages)

        table_controls = QWidget()
        table_controls_layout = QHBoxLayout(table_controls)
        table_controls_layout.setContentsMargins(0, 0, 0, 0)
        table_controls_layout.addWidget(self.search, 1)
        table_controls_layout.addWidget(self.date_sort_button)
        table_controls_layout.addWidget(self.export_button)

        self.body = QTextBrowser()
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.body.setStyleSheet("QTextBrowser { background: #f7f7f7; color: #111111; }")
        self.body.document().setDefaultStyleSheet(
            "html, body, div, p, span, table, td, th, pre { background-color: transparent; color: #111111; }"
            "a { color: #0645ad; }"
        )
        self.detail_tabs = QTabWidget()
        self.summary_table = self.make_table(["Field", "Value"])
        self.summary_widget = QSplitter(Qt.Vertical)
        self.summary_widget.addWidget(self.summary_table)
        self.summary_widget.addWidget(self.body)
        self.summary_widget.setStretchFactor(0, 1)
        self.summary_widget.setStretchFactor(1, 4)
        self.path_table = self.make_table(["Field", "Value"])
        self.categories_table = self.make_table(["Field", "Value"])
        self.database_table = self.make_table(["Column", "Value"])
        self.json_text = QTextEdit()
        self.json_text.setReadOnly(True)
        self.json_text.setLineWrapMode(QTextEdit.NoWrap)
        self.monospace_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.json_text.setFont(self.monospace_font)
        self.json_highlighter = JsonSourceHighlighter(self.json_text.document())

        self.detail_tabs.addTab(self.summary_widget, "Summary")
        self.detail_tabs.addTab(self.path_table, "Data File Path")
        self.detail_tabs.addTab(self.categories_table, "Categories")
        self.detail_tabs.addTab(self.database_table, "Mail Row")
        self.detail_tabs.addTab(self.json_text, "JSON")

        self.timeline = TimelineBarChart()
        self.timeline.timeFilterChanged.connect(self.apply_time_filter)
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)

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
        mail_layout = QVBoxLayout(mail_root)
        mail_layout.addWidget(self.timeline)
        mail_layout.addWidget(self.main_splitter)

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
        self.agenda_detail_table = self.make_table(["Field", "Value"])
        agenda_splitter = QSplitter(Qt.Vertical)
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
        agenda_splitter.addWidget(agenda_top)
        agenda_splitter.addWidget(self.agenda_detail_table)
        agenda_splitter.setStretchFactor(0, 5)
        agenda_splitter.setStretchFactor(1, 2)

        self.view_tabs = QTabWidget()
        self.view_tabs.addTab(mail_root, "Mail")
        self.view_tabs.addTab(agenda_splitter, "Agenda")
        self.setCentralWidget(self.view_tabs)

        self.update_optional_columns()
        self.update_agenda_columns()

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

    def clear_detail_tabs(self):
        """Clear all detail panes for the selected message."""
        self.set_table_rows(self.summary_table, [])
        self.set_table_rows(self.path_table, [])
        self.set_table_rows(self.categories_table, [])
        self.set_table_rows(self.database_table, [])
        self.json_text.clear()
        self.body.clear()

    def open_dialog(self):
        """Prompt the user to choose a PST or OST file."""
        path, _ = QFileDialog.getOpenFileName(self, "Open Outlook SQLite", "", "SQLite databases (*.sqlite *.db);;All files (*)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        """Open a mailbox file and reset the UI around its contents."""
        path = normalize_native_path(path)
        log_event(f"Opening Outlook SQLite file in UI: {path}")
        try:
            store = OutlookSqliteStore(path)
        except Exception as exc:
            log_error("Unable to open Outlook SQLite database", s(exc))
            QMessageBox.critical(self, "Unable to open Outlook SQLite database", s(exc))
            return

        if self.store:
            self.store.close()
        self.store = store
        self.opened_file_name = os.path.basename(path)
        self.time_filter = None
        self.current_folder_id = None
        self.current_folder_loaded_id = None
        self.current_folder_messages = []
        self.loaded_folder_messages_by_id = {}
        self.text_filter = ""
        self.agenda_text_filter = ""
        self.folder_items_by_id = {}
        self.model.clear_selection()
        self.search.clear()
        self.agenda_search.clear()
        self.clear_detail_tabs()
        self.set_table_rows(self.agenda_detail_table, [])
        log_event("Loading SQLite timeline timestamps")
        self.timeline.set_timeline_items(self.store.load_timeline_items())
        self.timeline.clear_overlay()
        self.timeline.clear_time_filter(emit=False)
        self.load_tree()
        self.load_agenda()
        log_event(f"Opened Outlook SQLite file in UI: {path}")

    def load_tree(self):
        """Load the folder tree and count columns for the current mailbox."""
        log_event("Rendering SQLite folder tree")
        sort_column = self.tree.header().sortIndicatorSection()
        sort_order = self.tree.header().sortIndicatorOrder()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self.folder_items_by_id = {}
        self.folders, self.folder_children = self.store.load_folders(self.time_filter, self.text_filter)
        db_total = sum(folder.direct_count for folder in self.folders.values())
        included_max = max((folder.included_count for folder in self.folders.values()), default=0)
        excluded_max = max((folder.excluded_count for folder in self.folders.values()), default=0)
        selected_item = None

        def sort_key(folder):
            """Return a folder sort key that keeps common folders in a useful order."""
            return (folder.folder_order or 0, folder_display_name(folder).lower(), folder.record_id)

        def top_level_sort_key(folder):
            """Return a sort key for top-level account and mailbox folders."""
            is_empty = folder.total_count == 0 and folder.direct_count == 0
            has_account_label = bool(s(folder.account_display_name).strip())
            return (
                1 if is_empty else 0,
                0 if has_account_label else 1,
                folder.folder_order or 0,
                folder_display_name(folder).lower(),
                folder.record_id,
            )

        def add(folder, parent=None):
            """Add a folder and its descendants to the tree widget."""
            nonlocal selected_item
            item = SortableTreeWidgetItem([
                folder_display_name(folder),
                str(folder.direct_count),
                str(folder.total_count),
                str(folder.included_count),
                str(folder.excluded_count),
            ])
            item.folder = folder
            self.folder_items_by_id[folder.record_id] = item
            if folder.record_id == self.current_folder_id:
                selected_item = item

            if parent:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)

            for child in sorted(self.folder_children.get(folder.record_id, []), key=sort_key):
                add(child, item)

            style_folder_count_cell(item, 1, folder.direct_count, db_total)
            style_folder_count_cell(item, 2, folder.total_count, db_total)
            style_folder_count_cell(item, 3, folder.included_count, included_max)
            style_folder_count_cell(item, 4, folder.excluded_count, excluded_max)
            style_folder_name_cell(item, folder_display_name(folder))
            item.setExpanded(folder.total_count > 0)

        roots = [folder for folder in self.folders.values() if folder.parent_id not in self.folders]
        for folder in sorted(roots, key=top_level_sort_key):
            add(folder)

        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.resizeColumnToContents(3)
        self.tree.resizeColumnToContents(4)
        self.set_initial_tree_width()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(sort_column, sort_order)
        log_event(
            f"Rendered SQLite folder tree: folders={len(self.folders)}, "
            f"direct_messages={sum(folder.direct_count for folder in self.folders.values())}"
        )

    def refresh_folder_filter_columns(self):
        """Refresh SQLite folder filter counts without rebuilding the tree."""
        if self.store is None or not self.folder_items_by_id:
            return
        self.folders, self.folder_children = self.store.load_folders(self.time_filter, self.text_filter)
        db_total = sum(folder.direct_count for folder in self.folders.values())
        included_max = max((folder.included_count for folder in self.folders.values()), default=0)
        excluded_max = max((folder.excluded_count for folder in self.folders.values()), default=0)
        for folder_id, item in self.folder_items_by_id.items():
            folder = self.folders.get(folder_id)
            if folder is None:
                continue
            item.folder = folder
            item.setText(1, str(folder.direct_count))
            item.setText(2, str(folder.total_count))
            item.setText(3, str(folder.included_count))
            item.setText(4, str(folder.excluded_count))
            style_folder_count_cell(item, 1, folder.direct_count, db_total)
            style_folder_count_cell(item, 2, folder.total_count, db_total)
            style_folder_count_cell(item, 3, folder.included_count, included_max)
            style_folder_count_cell(item, 4, folder.excluded_count, excluded_max)

    def set_initial_tree_width(self):
        """Size the folder tree columns and splitter for initial display."""
        count_width = 58
        self.tree.setColumnWidth(1, count_width)
        self.tree.setColumnWidth(2, count_width)
        self.tree.setColumnWidth(3, 72)
        self.tree.setColumnWidth(4, 72)

        folder_width = min(max(self.tree.columnWidth(0), 180), 340)
        self.tree.setColumnWidth(0, folder_width)

        tree_width = folder_width + (count_width * 2) + 144 + 24
        right_width = max(self.width() - tree_width, 800)
        self.main_splitter.setSizes([tree_width, right_width])

    def load_folder(self, item):
        """Start loading messages for the selected folder."""
        folder = item.folder
        if folder.record_id in self.loaded_folder_messages_by_id:
            log_event(
                f"Using cached SQLite folder load: folder_id={folder.record_id}, "
                f"name={folder_display_name(folder)!r}"
            )
            self.current_folder_id = folder.record_id
            self.current_folder_loaded_id = folder.record_id
            self.current_folder_messages = list(self.loaded_folder_messages_by_id[folder.record_id])
            self.refilter_current_folder_messages()
            return
        self.current_folder_id = folder.record_id
        self.current_folder_loaded_id = None
        log_event(f"Starting SQLite folder load: folder_id={folder.record_id}, name={folder_display_name(folder)!r}")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Loading...")
        self.clear_detail_tabs()

        try:
            messages = self.store.load_messages(folder.record_id)
        except Exception as exc:
            self.progress_bar.setVisible(False)
            self.progress_label.clear()
            log_error("Unable to load folder", s(exc))
            QMessageBox.critical(self, "Unable to load folder", s(exc))
            return

        self.progress_bar.setValue(100)
        self.progress_bar.setVisible(False)
        self.current_folder_messages = list(messages)
        self.current_folder_loaded_id = folder.record_id
        self.loaded_folder_messages_by_id[folder.record_id] = list(messages)
        filtered_messages = self.filter_messages_for_active_filters(self.current_folder_messages)
        if self.time_filter is None and not self.text_filter:
            self.progress_label.setText(f"Loaded {len(filtered_messages)} messages")
        else:
            self.progress_label.setText(f"Loaded {len(filtered_messages)}/{len(self.current_folder_messages)} messages matching filters")
        self.model.set(filtered_messages)
        self.update_optional_columns()
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))
            self.table.setFocus()
        else:
            self.clear_detail_tabs()
        self.update_timeline_overlay(folder, filtered_messages)
        log_event(
            f"Finished SQLite folder load: folder_id={folder.record_id}, "
            f"name={folder_display_name(folder)!r}, "
            f"messages={len(self.current_folder_messages)}, visible={len(filtered_messages)}"
        )

    def refilter_current_folder_messages(self):
        """Re-apply active filters to the already loaded SQLite folder."""
        if self.current_folder_id is None:
            self.timeline.clear_overlay()
            return
        filtered_messages = self.filter_messages_for_active_filters(self.current_folder_messages)
        if self.time_filter is None and not self.text_filter:
            self.progress_label.setText(f"Loaded {len(filtered_messages)} messages")
        else:
            self.progress_label.setText(
                f"Loaded {len(filtered_messages)}/{len(self.current_folder_messages)} messages matching filters"
            )
        self.model.set(filtered_messages)
        self.update_optional_columns()
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))
            self.table.setFocus()
        else:
            self.clear_detail_tabs()
        folder = self.folders.get(self.current_folder_id)
        if folder is not None:
            self.update_timeline_overlay(folder, filtered_messages)
        else:
            self.timeline.clear_overlay()

    def filter_messages_for_active_filters(self, messages):
        """Return messages matching the active time and text filters."""
        if self.time_filter is None and not self.text_filter:
            return messages
        start, end = self.time_filter if self.time_filter is not None else (None, None)
        filtered = []
        for message in messages:
            parsed = message.effective_datetime
            matches_time = self.time_filter is None or (parsed is not None and start <= parsed < end)
            matches_text = mail_matches_text(message.values, self.text_filter)
            if matches_time and matches_text:
                filtered.append(message)
        return filtered

    def apply_time_filter(self, start, end):
        """Apply time filter."""
        self.time_filter = (start, end) if start is not None and end is not None else None
        self.refresh_folder_filter_columns()
        if self.current_folder_id is not None:
            self.refilter_current_folder_messages()
        else:
            self.timeline.clear_overlay()

    def clear_time_filter(self):
        """Clear time filter."""
        self.timeline.clear_time_filter()

    def load_current_folder(self):
        """Load current folder."""
        if self.current_folder_id is None:
            return

        def find_item(item):
            """Find item."""
            if getattr(item, "folder", None) and item.folder.record_id == self.current_folder_id:
                return item
            for child_index in range(item.childCount()):
                found = find_item(item.child(child_index))
                if found:
                    return found
            return None

        for index in range(self.tree.topLevelItemCount()):
            item = find_item(self.tree.topLevelItem(index))
            if item:
                self.tree.setCurrentItem(item)
                self.load_folder(item)
                return
        self.timeline.clear_overlay()

    def update_timeline_overlay(self, folder, messages):
        """Update timeline overlay."""
        values = [message.effective_datetime for message in messages if message.effective_datetime is not None]
        if not values:
            self.timeline.clear_overlay()
            return
        self.timeline.set_overlay_timestamps(values, folder_display_name(folder))

    def update_detail_tabs(self, msg):
        """Populate all detail panes for a selected message."""
        folder = self.folders.get(msg.get("Record_FolderID")) if self.folders else None
        self.set_table_rows(
            self.summary_table,
            [
                ("Record ID", msg.record_id),
                ("Folder", folder.path if folder is not None else ""),
                ("Subject", msg.subject),
                ("From", msg.sender),
                ("To", msg.to),
                ("CC", msg.cc),
                ("Date Received", format_timestamp(msg.get("Message_TimeReceived"))),
                ("Date Sent", format_timestamp(msg.get("Message_TimeSent"))),
                ("Message ID", msg.get("Message_MessageID")),
                ("Conversation ID", msg.get("Conversation_ConversationID")),
                ("Thread ID", msg.get("Threads_ThreadID")),
                ("Categories", msg.category_names),
                ("Has Attachment", format_bool(msg.get("Message_HasAttachment"))),
                ("Read", format_bool(msg.get("Message_ReadFlag"))),
                ("Size", format_bytes(msg.get("Message_Size"))),
            ],
        )

        self.set_table_rows(
            self.path_table,
            [
                ("Message Data File", msg.path),
                ("Folder ID", msg.get("Record_FolderID")),
                ("Database", self.store.path if self.store else ""),
            ],
        )

        category_rows = []
        for index, category in enumerate(msg.categories, 1):
            prefix = f"Category {index}"
            category_rows.extend(
                [
                    (f"{prefix} Name", category.get("Category_Name")),
                    (f"{prefix} Record ID", category.get("Category_RecordID")),
                    (f"{prefix} Data File", category.get("PathToDataFile")),
                    (f"{prefix} Color", category.get("Category_BackgroundColorHex")),
                    (f"{prefix} Exchange GUID", category.get("Cateogry_ExchangeGuid")),
                ]
            )
        self.set_table_rows(self.categories_table, category_rows)

        self.set_table_rows(self.database_table, [(key, value) for key, value in msg.values.items()])
        description = dict(msg.values)
        description["CategoryDetails"] = msg.categories
        self.json_text.setPlainText(json.dumps(description, indent=2, ensure_ascii=False, default=s))

    def load_agenda(self):
        """Load the agenda view for the current SQLite database."""
        if self.store is None:
            self.agenda_model.set([])
            self.agenda_calendar.set_items([])
            return
        try:
            items = self.store.load_agenda_items()
        except Exception as exc:
            log_error("Unable to load agenda", s(exc))
            QMessageBox.warning(self, "Unable to load agenda", s(exc))
            items = []
        self.agenda_model.set(items)
        self.apply_agenda_filter()
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
            value = details[key]
            if isinstance(value, (bytes, bytearray)):
                value = value.hex()
            rows.append((key, value))
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
        self.agenda_table.setColumnWidth(AgendaModel.TYPE_COLUMN, 120)
        self.agenda_table.setColumnWidth(AgendaModel.FOLDER_COLUMN, 140)
        self.agenda_table.setColumnWidth(AgendaModel.SOURCE_COLUMN, 120)

    def on_select(self, idx):
        """Handle select."""
        msg = self.model.msg(idx.row())
        if not msg:
            return
        self.update_detail_tabs(msg)
        preview = s(msg.preview).strip()
        self.body.setPlainText(preview or "(No message preview found in the database)")

    def toggle_date_sort(self, checked):
        """Toggle the message table between newest-first and oldest-first sorting."""
        order = Qt.DescendingOrder if checked else Qt.AscendingOrder
        self.table.sortByColumn(MsgModel.DATE_COLUMN, order)
        self.date_sort_button.setText("Date: Newest" if checked else "Date: Oldest")
        self.update_optional_columns()
        if self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(0, 0))

    def schedule_search(self):
        """Debounce text search after the query changes."""
        self.search_timer.start()

    def apply_text_filter(self):
        """Apply text filter."""
        self.text_filter = self.search.text().lower().strip()
        if self.store is None:
            return
        self.refresh_folder_filter_columns()
        if self.current_folder_id is not None:
            self.refilter_current_folder_messages()
        else:
            self.timeline.clear_overlay()

    def update_optional_columns(self):
        """Update optional columns."""
        if not hasattr(self, "table"):
            return
        self.table.setColumnHidden(MsgModel.CC_COLUMN, not self.model.has_cc())
        self.table.setColumnHidden(MsgModel.CATEGORIES_COLUMN, not self.model.has_categories())
        self.table.setColumnWidth(MsgModel.EXPORT_COLUMN, 58)
        self.table.setColumnWidth(MsgModel.SUBJECT_COLUMN, 320)
        self.table.setColumnWidth(2, 180)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(MsgModel.CATEGORIES_COLUMN, 180)
        self.table.setColumnWidth(MsgModel.DATE_COLUMN, 150)
        self.table.setColumnWidth(MsgModel.ATTACHMENT_COLUMN, 82)
        self.table.setColumnWidth(MsgModel.READ_COLUMN, 64)
        self.table.setColumnWidth(MsgModel.EMAIL_SIZE_COLUMN, 84)
        self.table.setColumnWidth(MsgModel.PATH_COLUMN, 360)

    def export_selected_messages(self):
        """Export checked SQLite messages into a reporting zip."""
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
                for index, msg in enumerate(messages, 1):
                    folder = export_folder_name(index, msg.subject, msg.date_value)
                    description = dict(msg.values)
                    description["CategoryDetails"] = msg.categories
                    smtp_headers = "\n".join(
                        f"{label}: {value}"
                        for label, value in (
                            ("From", msg.sender),
                            ("To", msg.to),
                            ("Cc", msg.cc),
                            ("Subject", msg.subject),
                            ("Date", msg.date_text),
                            ("Message-ID", msg.get("Message_MessageID")),
                        )
                        if s(value)
                    )
                    preview = s(msg.preview).strip() or "(No message preview found in the database)"
                    summary_rows = (
                        ("Record ID", msg.record_id),
                        ("Subject", msg.subject),
                        ("From", msg.sender),
                        ("To", msg.to),
                        ("CC", msg.cc),
                        ("Date Received", format_timestamp(msg.get("Message_TimeReceived"))),
                        ("Date Sent", format_timestamp(msg.get("Message_TimeSent"))),
                        ("Message ID", msg.get("Message_MessageID")),
                        ("Conversation ID", msg.get("Conversation_ConversationID")),
                        ("Thread ID", msg.get("Threads_ThreadID")),
                        ("Categories", msg.category_names),
                        ("Has Attachment", format_bool(msg.get("Message_HasAttachment"))),
                        ("Read", format_bool(msg.get("Message_ReadFlag"))),
                        ("Size", format_bytes(msg.get("Message_Size"))),
                    )
                    rendered = exported_html_document(summary_rows, f"<pre>{html_lib.escape(preview)}</pre>")
                    archive.writestr(f"{folder}/smtp-headers.txt", smtp_headers)
                    archive.writestr(
                        f"{folder}/message.json",
                        json.dumps(description, indent=2, ensure_ascii=False, default=s),
                    )
                    archive.writestr(f"{folder}/message.html", rendered)
        except Exception as exc:
            log_error("Unable to export selected emails", s(exc))
            QMessageBox.critical(self, "Unable to export selected emails", s(exc))
            return

        QMessageBox.information(self, "Export emails", f"Exported {len(messages)} emails to:\n{path}")

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

    def closeEvent(self, event):
        """Handle the Qt close event and release resources."""
        if self.store:
            self.store.close()
        super().closeEvent(event)


def main(argv=None):
    """Run the Outlook SQLite reader UI."""
    argv = list(sys.argv[1:] if argv is None else argv)
    suppress_qt_font_warnings()
    app = QApplication([sys.argv[0], *argv])
    apply_qt_app_icon(app)
    path = argv[0] if argv else None
    if path is None and os.path.exists("Outlook.sqlite"):
        path = "Outlook.sqlite"

    window = MacOutlookReader(initial_file=path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
