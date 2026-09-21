#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    from PySide6.QtCore import (
        QAbstractTableModel,
        QModelIndex,
        QPoint,
        QPointF,
        QRectF,
        Qt,
        QTimer,
        QUrl,
        Signal,
    )
    from PySide6.QtGui import (
        QAction,
        QColor,
        QDesktopServices,
        QFont,
        QIcon,
        QKeySequence,
        QPainter,
        QPainterPath,
        QPen,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStatusBar,
        QTableView,
        QAbstractItemView,
        QToolTip,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name != "PySide6":
        raise
    print("PySide6 is not installed. Run: python -m pip install -r requirements.txt")
    raise SystemExit(1) from exc

from history_reader import HistoryReader, HistoryRecord, iso_timestamp


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY_PATH = SCRIPT_DIR / "History"
APP_ICON_PATH = SCRIPT_DIR / "app_icon.ico"
UNKNOWN_DOMAIN = "(unknown)"


def default_profile_folder(browser: str) -> Path:
    home = Path.home()
    if sys.platform.startswith("win"):
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        if browser == "chrome":
            return local_app_data / "Google" / "Chrome" / "User Data" / "Default"
        return local_app_data / "Microsoft" / "Edge" / "User Data" / "Default"

    if sys.platform == "darwin":
        if browser == "chrome":
            return home / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
        return home / "Library" / "Application Support" / "Microsoft Edge" / "Default"

    if browser == "chrome":
        chrome = home / ".config" / "google-chrome" / "Default"
        if chrome.exists():
            return chrome
        return home / ".config" / "chromium" / "Default"
    return home / ".config" / "microsoft-edge" / "Default"


def app_icon() -> QIcon:
    if APP_ICON_PATH.exists():
        return QIcon(str(APP_ICON_PATH))
    png_icon = SCRIPT_DIR / "icon_history.png"
    if png_icon.exists():
        return QIcon(str(png_icon))
    return QIcon()


def record_domain(record: HistoryRecord) -> str:
    return extract_hostname(record.url) or extract_hostname(record.source) or UNKNOWN_DOMAIN


def extract_hostname(value: str) -> str | None:
    if not value:
        return None

    candidate = value.strip()
    if candidate.startswith("blob:"):
        candidate = candidate[5:]

    parsed = urlparse(candidate)
    if not parsed.scheme and "://" not in candidate:
        parsed = urlparse(f"https://{candidate}")

    host = parsed.hostname
    if host:
        return host.lower()
    return None


def domain_color(domain: str) -> QColor:
    digest = hashlib.sha256(domain.encode("utf-8", errors="ignore")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    saturation = 95 + digest[2] % 45
    value = 150 + digest[3] % 55
    return QColor.fromHsv(hue, saturation, value)


def readable_text_color(background: QColor) -> QColor:
    return QColor("#0f172a") if background.lightness() > 120 else QColor("#f8fafc")


class HistoryTableModel(QAbstractTableModel):
    HEADERS = [
        "Timestamp",
        "Type",
        "Title",
        "URL",
        "Source / Referrer",
        "Visits",
        "Typed",
        "Details",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.records: list[HistoryRecord] = []

    def set_records(self, records: list[HistoryRecord]) -> None:
        self.beginResetModel()
        self.records = records
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        record = self.records[index.row()]
        if role == Qt.DisplayRole:
            values = [
                record.iso_time,
                record.kind,
                record.title,
                record.url,
                record.source,
                "" if record.visit_count is None else str(record.visit_count),
                "" if record.typed_count is None else str(record.typed_count),
                record.details,
            ]
            return values[index.column()]

        if role == Qt.BackgroundRole and index.column() == 1:
            return domain_color(record_domain(record))

        if role == Qt.ForegroundRole and index.column() == 1:
            return readable_text_color(domain_color(record_domain(record)))

        if role == Qt.FontRole and record.kind == "Download" and index.column() == 1:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ToolTipRole:
            return f"{record.kind}: {record.url}\nDomain: {record_domain(record)}\n{record.details}"

        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None


class FilteredHistoryTableModel(QAbstractTableModel):
    def __init__(self, source: HistoryTableModel) -> None:
        super().__init__()
        self.source = source
        self.query = ""
        self.regex: re.Pattern[str] | None = None
        self.regex_error: str | None = None
        self.time_start: int | None = None
        self.time_end: int | None = None
        self.rows: list[int] = []
        self.sort_column = 0
        self.sort_order = Qt.DescendingOrder
        self.source.modelReset.connect(self.rebuild)

    def set_filter(self, query: str) -> None:
        self.query = query.strip()
        self.regex_error = None
        if self.query:
            try:
                self.regex = re.compile(self.query, re.IGNORECASE)
            except re.error as exc:
                self.regex = None
                self.regex_error = str(exc)
        else:
            self.regex = None
        self.rebuild()

    def set_time_range(self, start: int | None, end: int | None) -> None:
        self.time_start = start
        self.time_end = end
        self.rebuild()

    def clear_time_range(self) -> None:
        self.set_time_range(None, None)

    def rebuild(self) -> None:
        self.beginResetModel()
        self.rows = [
            index
            for index, record in enumerate(self.source.records)
            if self._matches_query(record) and self._matches_time(record)
        ]
        self._sort_rows()
        self.endResetModel()

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        self.sort_column = column
        self.sort_order = order
        self._sort_rows()
        self.layoutChanged.emit()

    def _sort_rows(self) -> None:
        reverse = self.sort_order == Qt.DescendingOrder
        if self.sort_column == 0:
            none_sentinel = -1 if reverse else 10**30
            self.rows.sort(
                key=lambda row_index: (
                    self.source.records[row_index].timestamp
                    if self.source.records[row_index].timestamp is not None
                    else none_sentinel
                ),
                reverse=reverse,
            )
            return

        self.rows.sort(
            key=lambda row_index: self._sort_key(self.source.records[row_index]),
            reverse=reverse,
        )

    def _sort_key(self, record: HistoryRecord):
        if self.sort_column == 0:
            return record.timestamp or 0
        if self.sort_column == 1:
            return record.kind.lower()
        if self.sort_column == 2:
            return record.title.lower()
        if self.sort_column == 3:
            return record.url.lower()
        if self.sort_column == 4:
            return record.source.lower()
        if self.sort_column == 5:
            return -1 if record.visit_count is None else record.visit_count
        if self.sort_column == 6:
            return -1 if record.typed_count is None else record.typed_count
        if self.sort_column == 7:
            return record.details.lower()
        return ""

    def _matches_query(self, record: HistoryRecord) -> bool:
        if self.regex_error:
            return False
        if self.regex is None:
            return True
        return self.regex.search(self._search_text(record)) is not None

    def _matches_time(self, record: HistoryRecord) -> bool:
        if self.time_start is None or self.time_end is None:
            return True
        if record.timestamp is None:
            return False
        return self.time_start <= record.timestamp <= self.time_end

    def _search_text(self, record: HistoryRecord) -> str:
        return " ".join(
            [
                record.iso_time,
                record.kind,
                record.title,
                record.url,
                record.source,
                record.details,
            ]
        )

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return self.source.columnCount(parent)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        source_index = self.source.index(self.rows[index.row()], index.column())
        return self.source.data(source_index, role)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ):
        return self.source.headerData(section, orientation, role)

    def filtered_records(self) -> list[HistoryRecord]:
        return [self.source.records[row] for row in self.rows]

    def query_matched_records(self) -> list[HistoryRecord]:
        return [
            record
            for record in self.source.records
            if self._matches_query(record)
        ]


class HistogramWidget(QWidget):
    rangeSelected = Signal(object, object)
    rangeCleared = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.records: list[HistoryRecord] = []
        self.window_start: int | None = None
        self.window_end: int | None = None
        self.drag_start: QPoint | None = None
        self.drag_current: QPoint | None = None
        self.buckets: list[int] = []
        self.bucket_domains: list[dict[str, int]] = []
        self.setMinimumHeight(150)
        self.setMouseTracking(True)

    def set_records(
        self,
        records: list[HistoryRecord],
        window_start: int | None = None,
        window_end: int | None = None,
    ) -> None:
        self.records = records
        self.window_start = window_start
        self.window_end = window_end
        self.buckets = []
        self.bucket_domains = []
        self.drag_start = None
        self.drag_current = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._plot_rect()
        painter.fillRect(self.rect(), QColor("#f8fafc"))

        if not self.records:
            painter.setPen(QColor("#64748b"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Open a History file to view activity")
            return

        domain_buckets = self._bucket_domain_counts()
        buckets = [sum(bucket.values()) for bucket in domain_buckets]
        self.bucket_domains = domain_buckets
        self.buckets = buckets
        max_count = max(max(buckets), 1) if buckets else 1
        bar_gap = 2
        bar_width = max(2, (rect.width() - (len(buckets) - 1) * bar_gap) / len(buckets))

        baseline = rect.bottom()
        self._draw_grid_and_ticks(painter, rect)

        for index, count in enumerate(buckets):
            if count <= 0:
                continue
            x = rect.left() + index * (bar_width + bar_gap)
            stack_bottom = baseline
            for domain, domain_count in self._stacked_domains(domain_buckets[index]):
                height = (domain_count / max_count) * rect.height()
                bar_rect = QRectF(x, stack_bottom - height, bar_width, height)
                painter.fillRect(bar_rect, domain_color(domain))
                stack_bottom -= height

        self._draw_drag_selection(painter, rect)
        painter.setPen(QColor("#334155"))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        start, end = self._display_range_datetimes()
        painter.drawText(14, self.height() - 7, start)
        painter.drawText(
            self.width() - 180,
            self.height() - 7,
            166,
            16,
            Qt.AlignRight,
            end,
        )

        path = QPainterPath()
        points = self._sparkline_points(rect, buckets, max_count)
        if points:
            path.moveTo(points[0])
            for point in points[1:]:
                path.lineTo(point)
            painter.setPen(QPen(QColor("#0f172a"), 1.5))
            painter.drawPath(path)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._plot_rect().contains(event.position().toPoint()):
            self.drag_start = event.position().toPoint()
            self.drag_current = self.drag_start
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_start is not None:
            self.drag_current = event.position().toPoint()
            self.update()
        else:
            self._show_bucket_tooltip(event.position().toPoint(), event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self.drag_start is None:
            return

        start_x = self.drag_start.x()
        end_x = event.position().toPoint().x()
        self.drag_start = None
        self.drag_current = None

        if abs(end_x - start_x) < 8:
            self.update()
            return

        start_time = self._time_at_x(min(start_x, end_x))
        end_time = self._time_at_x(max(start_x, end_x))
        if start_time is not None and end_time is not None and start_time < end_time:
            self.rangeSelected.emit(start_time, end_time)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.rangeCleared.emit()

    def wheelEvent(self, event) -> None:
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return

        current_start, current_end = self._display_range()
        span = max(1, current_end - current_start)
        factor = 0.55 if event.angleDelta().y() > 0 else 1.8
        center = self._time_at_x(round(event.position().x())) or ((current_start + current_end) // 2)
        new_span = min(full_end - full_start, max(60_000_000, int(span * factor)))
        left_weight = (center - current_start) / span
        new_start = int(center - new_span * left_weight)
        new_end = new_start + new_span

        if new_start < full_start:
            new_end += full_start - new_start
            new_start = full_start
        if new_end > full_end:
            new_start -= new_end - full_end
            new_end = full_end
        new_start = max(full_start, new_start)

        if new_start <= full_start and new_end >= full_end:
            self.rangeCleared.emit()
        else:
            self.rangeSelected.emit(new_start, new_end)

    def zoom_out(self) -> None:
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return

        current_start, current_end = self._display_range()
        span = current_end - current_start
        new_span = min(full_end - full_start, span * 2)
        center = (current_start + current_end) // 2
        new_start = max(full_start, center - new_span // 2)
        new_end = min(full_end, new_start + new_span)
        new_start = max(full_start, new_end - new_span)

        if new_start <= full_start and new_end >= full_end:
            self.rangeCleared.emit()
        else:
            self.rangeSelected.emit(int(new_start), int(new_end))

    def _bucket_counts(self) -> list[int]:
        return [sum(bucket.values()) for bucket in self._bucket_domain_counts()]

    def _bucket_domain_counts(self) -> list[dict[str, int]]:
        start, end = self._display_range()
        span = max(1, end - start)
        bucket_count = self._bucket_count()
        buckets: list[dict[str, int]] = [{} for _ in range(bucket_count)]
        for record in self.records:
            timestamp = record.timestamp
            if not timestamp or timestamp < start or timestamp > end:
                continue
            index = min(bucket_count - 1, int(((timestamp - start) / span) * bucket_count))
            domain = record_domain(record)
            buckets[index][domain] = buckets[index].get(domain, 0) + 1
        return buckets

    def _stacked_domains(self, domains: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(domains.items(), key=lambda item: (-item[1], item[0]))

    def _sparkline_points(
        self, rect, buckets: list[int], max_count: int
    ) -> list[QPointF]:
        if not buckets:
            return []
        step = rect.width() / max(1, len(buckets) - 1)
        return [
            QPointF(
                rect.left() + index * step,
                rect.bottom() - ((count / max_count) * rect.height()),
            )
            for index, count in enumerate(buckets)
        ]

    def _range_datetimes(self) -> tuple[str, str]:
        start, end = self._full_range()
        if start is None or end is None:
            return "", ""
        return iso_timestamp(start), iso_timestamp(end)

    def _display_range_datetimes(self) -> tuple[str, str]:
        start, end = self._display_range()
        return iso_timestamp(start), iso_timestamp(end)

    def _display_range(self) -> tuple[int, int]:
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return 0, 1
        if self.window_start is None or self.window_end is None:
            return full_start, full_end
        return self.window_start, self.window_end

    def _full_range(self) -> tuple[int | None, int | None]:
        timestamps = [record.timestamp for record in self.records if record.timestamp]
        if not timestamps:
            return None, None
        return min(timestamps), max(timestamps)

    def _visible_timestamps(self) -> list[int]:
        start, end = self._display_range()
        return [
            record.timestamp
            for record in self.records
            if record.timestamp and start <= record.timestamp <= end
        ]

    def _bucket_count(self) -> int:
        width = max(1, self._plot_rect().width())
        return max(16, min(180, width // 10))

    def _plot_rect(self):
        return self.rect().adjusted(14, 12, -14, -32)

    def _time_at_x(self, x: int) -> int | None:
        rect = self._plot_rect()
        if rect.width() <= 0:
            return None
        start, end = self._display_range()
        ratio = (min(max(x, rect.left()), rect.right()) - rect.left()) / rect.width()
        return int(start + (end - start) * ratio)

    def _draw_drag_selection(self, painter: QPainter, rect) -> None:
        if self.drag_start is None or self.drag_current is None:
            return
        left = min(self.drag_start.x(), self.drag_current.x())
        right = max(self.drag_start.x(), self.drag_current.x())
        selection = QRectF(
            max(rect.left(), left),
            rect.top(),
            min(rect.right(), right) - max(rect.left(), left),
            rect.height(),
        )
        painter.fillRect(selection, QColor(37, 99, 235, 50))
        painter.setPen(QPen(QColor("#1d4ed8"), 1))
        painter.drawRect(selection)

    def _draw_grid_and_ticks(self, painter: QPainter, rect) -> None:
        ticks = self._time_ticks(max(3, min(8, rect.width() // 180)))
        painter.setPen(QPen(QColor("#e2e8f0"), 1))
        for timestamp in ticks:
            x = self._x_for_time(timestamp)
            painter.drawLine(x, rect.top(), x, rect.bottom())

        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        painter.setPen(QColor("#475569"))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        for timestamp in ticks:
            x = self._x_for_time(timestamp)
            painter.drawLine(x, rect.bottom(), x, rect.bottom() + 4)
            label = self._tick_label(timestamp)
            painter.drawText(
                int(x - 55),
                rect.bottom() + 6,
                110,
                18,
                Qt.AlignCenter,
                label,
            )

    def _time_ticks(self, target_count: int) -> list[int]:
        start, end = self._display_range()
        span_seconds = max(1, (end - start) / 1_000_000)
        step_seconds = self._nice_step(span_seconds / max(1, target_count - 1))
        first = int(((start / 1_000_000 + step_seconds - 1) // step_seconds) * step_seconds)
        ticks = []
        current = first
        end_seconds = end / 1_000_000
        while current <= end_seconds and len(ticks) < 20:
            ticks.append(int(current * 1_000_000))
            current += step_seconds
        return ticks

    def _nice_step(self, raw_seconds: float) -> int:
        steps = [
            60,
            5 * 60,
            15 * 60,
            30 * 60,
            60 * 60,
            3 * 60 * 60,
            6 * 60 * 60,
            12 * 60 * 60,
            24 * 60 * 60,
            2 * 24 * 60 * 60,
            7 * 24 * 60 * 60,
            14 * 24 * 60 * 60,
            30 * 24 * 60 * 60,
            90 * 24 * 60 * 60,
            180 * 24 * 60 * 60,
            365 * 24 * 60 * 60,
        ]
        return min(steps, key=lambda step: abs(step - raw_seconds))

    def _x_for_time(self, timestamp: int) -> int:
        rect = self._plot_rect()
        start, end = self._display_range()
        span = max(1, end - start)
        ratio = (timestamp - start) / span
        return int(rect.left() + ratio * rect.width())

    def _tick_label(self, timestamp: int) -> str:
        start, end = self._display_range()
        span_seconds = (end - start) / 1_000_000
        value = dt.datetime.fromtimestamp(
            (timestamp - 11644473600000000) / 1_000_000,
            tz=dt.timezone.utc,
        ).astimezone()

        if span_seconds <= 24 * 60 * 60:
            return value.strftime("%H:%M")
        if span_seconds <= 14 * 24 * 60 * 60:
            return value.strftime("%b %d %H:%M")
        if span_seconds <= 180 * 24 * 60 * 60:
            return value.strftime("%b %d")
        return value.strftime("%Y-%m")

    def _show_bucket_tooltip(self, pos: QPoint, global_pos: QPoint) -> None:
        rect = self._plot_rect()
        if not rect.contains(pos) or not self.buckets:
            QToolTip.hideText()
            return

        index = self._bucket_index_at_x(pos.x())
        if index is None:
            QToolTip.hideText()
            return

        count = self.buckets[index]
        bucket_start, bucket_end = self._bucket_range(index)
        top_domains = self._stacked_domains(self.bucket_domains[index])[:6]
        domain_lines = "\n".join(
            f"{domain}: {domain_count:,}" for domain, domain_count in top_domains
        )
        label = (
            f"{count:,} item{'s' if count != 1 else ''}\n"
            f"{iso_timestamp(bucket_start)}\n"
            f"to {iso_timestamp(bucket_end)}"
        )
        if domain_lines:
            label += f"\n\n{domain_lines}"
        QToolTip.showText(global_pos, label, self)

    def _bucket_index_at_x(self, x: int) -> int | None:
        rect = self._plot_rect()
        if rect.width() <= 0 or not self.buckets:
            return None
        ratio = (min(max(x, rect.left()), rect.right()) - rect.left()) / rect.width()
        return min(len(self.buckets) - 1, max(0, int(ratio * len(self.buckets))))

    def _bucket_range(self, index: int) -> tuple[int, int]:
        start, end = self._display_range()
        span = max(1, end - start)
        bucket_span = span / max(1, len(self.buckets))
        bucket_start = int(start + index * bucket_span)
        bucket_end = int(min(end, bucket_start + bucket_span))
        return bucket_start, bucket_end


class HistoryTableView(QTableView):
    copyRequested = Signal()
    filesDropped = Signal(object)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.Copy):
            self.copyRequested.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            self.filesDropped.emit(event.mimeData().urls())
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Chromium History Browser")
        self.setWindowIcon(app_icon())
        self.resize(1320, 780)
        self.setAcceptDrops(True)
        self.current_path: Path | None = None

        self.source_model = HistoryTableModel()
        self.filtered_model = FilteredHistoryTableModel(self.source_model)

        self.histogram = HistogramWidget()
        self.histogram.rangeSelected.connect(self._set_time_range)
        self.histogram.rangeCleared.connect(self._clear_time_range)
        self.table = HistoryTableView()
        self.table.setAcceptDrops(True)
        self.table.setModel(self.filtered_model)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setStyleSheet(
            """
            QTableView::item:hover:!selected {
                background: transparent;
            }
            """
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 260)
        self.table.setColumnWidth(3, 360)
        self.table.setColumnWidth(4, 260)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.table.copyRequested.connect(self.copy_selected_cells)
        self.table.filesDropped.connect(self.load_dropped_urls)

        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Regex filter across timestamp, title, URL, referrer, or details"
        )
        self.search.textChanged.connect(self._on_filter_changed)

        open_button = QPushButton("Open Different File")
        open_button.clicked.connect(self.open_file_dialog)
        zoom_out_button = QPushButton("Zoom Out")
        zoom_out_button.clicked.connect(self.histogram.zoom_out)
        reset_zoom_button = QPushButton("Reset Timeline")
        reset_zoom_button.clicked.connect(self._clear_time_range)
        self.range_label = QLabel("All activity")

        toolbar = QHBoxLayout()
        toolbar.addWidget(open_button)
        toolbar.addWidget(QLabel("Filter"))
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(zoom_out_button)
        toolbar.addWidget(reset_zoom_button)
        toolbar.addWidget(self.range_label)

        splitter = QSplitter(Qt.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.histogram)
        splitter.addWidget(top)
        splitter.addWidget(self.table)
        splitter.setSizes([170, 610])

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.setStatusBar(QStatusBar())
        self._create_menu()

        startup_path = initial_path or self._default_history_path()
        if startup_path is not None:
            self.load_history(startup_path)
        else:
            QTimer.singleShot(0, self.open_file_dialog)

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_action = QAction("&Open Different History File", self)
        open_action.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_action)

        chrome_folder_action = QAction("Open &Chrome History Folder", self)
        chrome_folder_action.triggered.connect(
            lambda: self.open_browser_history_folder("chrome")
        )
        file_menu.addAction(chrome_folder_action)

        edge_folder_action = QAction("Open &Edge History Folder", self)
        edge_folder_action.triggered.connect(
            lambda: self.open_browser_history_folder("edge")
        )
        file_menu.addAction(edge_folder_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        copy_action = QAction("&Copy Selected Cells", self)
        copy_action.triggered.connect(self.copy_selected_cells)
        edit_menu.addAction(copy_action)

    def copy_selected_cells(self) -> None:
        indexes = self.table.selectedIndexes()
        if not indexes:
            return

        indexes = sorted(indexes, key=lambda index: (index.row(), index.column()))
        min_row = min(index.row() for index in indexes)
        max_row = max(index.row() for index in indexes)
        min_col = min(index.column() for index in indexes)
        max_col = max(index.column() for index in indexes)
        selected = {(index.row(), index.column()): index for index in indexes}

        lines = []
        for row in range(min_row, max_row + 1):
            values = []
            for col in range(min_col, max_col + 1):
                index = selected.get((row, col))
                value = "" if index is None else self.filtered_model.data(index, Qt.DisplayRole)
                values.append(self._clipboard_safe(value))
            lines.append("\t".join(values))

        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(
            f"Copied {len(indexes):,} selected cell{'s' if len(indexes) != 1 else ''}"
        )

    def _clipboard_safe(self, value) -> str:
        if value is None:
            return ""
        return str(value).replace("\r\n", " ").replace("\n", " ").replace("\t", " ")

    def open_browser_history_folder(self, browser: str) -> None:
        folder = default_profile_folder(browser)
        if not folder.exists():
            QMessageBox.warning(
                self,
                "History folder not found",
                f"Could not find the default {browser.title()} profile folder:\n{folder}",
            )
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(
                self,
                "Could not open folder",
                f"The operating system did not open:\n{folder}",
            )

    def dragEnterEvent(self, event) -> None:
        if self._history_path_from_drop(event.mimeData().urls()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._history_path_from_drop(event.mimeData().urls()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        history_path = self.load_dropped_urls(event.mimeData().urls())
        if history_path is None:
            event.ignore()
            return
        event.acceptProposedAction()

    def load_dropped_urls(self, urls: list[QUrl]) -> Path | None:
        history_path = self._history_path_from_drop(urls)
        if history_path is None:
            return None
        self.load_history(history_path)
        return history_path

    def _history_path_from_drop(self, urls: object) -> Path | None:
        for url in urls:
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            history_path = self._history_path_from_path(path)
            if history_path is not None:
                return history_path
        return None

    def _history_path_from_path(self, path: Path) -> Path | None:
        if path.is_dir():
            candidate = path / "History"
            if candidate.is_file():
                return candidate
            return None
        if path.is_file():
            return path
        return None

    def _default_history_path(self) -> Path | None:
        if DEFAULT_HISTORY_PATH.exists() and DEFAULT_HISTORY_PATH.is_file():
            return DEFAULT_HISTORY_PATH
        return None

    def open_file_dialog(self) -> None:
        start_dir = self.current_path.parent if self.current_path else SCRIPT_DIR
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Chromium History SQLite file",
            str(start_dir),
            "History SQLite files (History *.sqlite *.db *.*)",
        )
        if path:
            self.load_history(Path(path))

    def load_history(self, path: Path) -> None:
        self._clear_loaded_data()
        self.statusBar().showMessage(f"Loading {path}...")
        try:
            records = HistoryReader(path).read_records()
        except Exception as exc:
            QMessageBox.critical(self, "Could not open History file", str(exc))
            self._show_status("No History file loaded")
            return

        self.current_path = path
        self.source_model.set_records(records)
        self.filtered_model.rebuild()
        self._refresh_histogram()
        downloads = sum(1 for record in records if record.kind == "Download")
        visits = len(records) - downloads
        self._set_range_label()
        self._show_status(
            f"Loaded {len(records):,} records: {visits:,} visits, {downloads:,} downloads from {path}"
        )

    def _clear_loaded_data(self) -> None:
        self.table.clearSelection()
        self.current_path = None
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.filtered_model.query = ""
        self.filtered_model.regex = None
        self.filtered_model.regex_error = None
        self.filtered_model.clear_time_range()
        self.source_model.set_records([])
        self.histogram.set_records([])
        self._set_range_label()

    def _on_filter_changed(self, query: str) -> None:
        self.filtered_model.set_filter(query)
        self._refresh_histogram()
        self._show_status()

    def _set_time_range(self, start: int, end: int) -> None:
        self.filtered_model.set_time_range(start, end)
        self._refresh_histogram()
        self._set_range_label()
        self._show_status()

    def _clear_time_range(self) -> None:
        self.filtered_model.clear_time_range()
        self._refresh_histogram()
        self._set_range_label()
        self._show_status()

    def _refresh_histogram(self) -> None:
        self.histogram.set_records(
            self.filtered_model.query_matched_records(),
            self.filtered_model.time_start,
            self.filtered_model.time_end,
        )

    def _set_range_label(self) -> None:
        if self.filtered_model.time_start is None or self.filtered_model.time_end is None:
            self.range_label.setText("All activity")
            return
        self.range_label.setText(
            f"{iso_timestamp(self.filtered_model.time_start)} to {iso_timestamp(self.filtered_model.time_end)}"
        )

    def _show_status(self, prefix: str | None = None) -> None:
        if self.filtered_model.regex_error:
            self.statusBar().showMessage(f"Invalid regex: {self.filtered_model.regex_error}")
            return

        message = (
            f"Showing {self.filtered_model.rowCount():,} of "
            f"{len(self.filtered_model.query_matched_records()):,} matching records"
        )
        if prefix:
            message = f"{prefix} | {message}"
        self.statusBar().showMessage(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse Chromium History SQLite files.")
    parser.add_argument("history_file", nargs="?", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    window = MainWindow(args.history_file)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
