from collections import Counter
from datetime import datetime, timedelta
import hashlib
from math import ceil

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget


MIN_VALID_DATE = datetime(1971, 1, 2)
IGNORED_SENTINEL_DATES = {
    datetime(2001, 1, 1).date(),
}


def normalize_message_datetime(value):
    """Normalize message datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed if is_valid_message_datetime(parsed) else None

    try:
        timestamp = float(value)
    except Exception:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed if is_valid_message_datetime(parsed) else None

    try:
        parsed = datetime.fromtimestamp(timestamp)
    except Exception:
        return None
    return parsed if is_valid_message_datetime(parsed) else None


def is_valid_message_datetime(parsed):
    """Return whether valid message datetime."""
    return parsed >= MIN_VALID_DATE and parsed.date() not in IGNORED_SENTINEL_DATES


def folder_leaf(value):
    """Return a compact folder label for grouped timeline colors."""
    text = str(value or "").strip()
    if not text:
        return "(unknown folder)"
    text = text.replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    return parts[-1] if parts else text


def folder_color(folder_name):
    """Return a stable readable color for a folder name."""
    key = folder_leaf(folder_name).casefold()
    digest = hashlib.sha256(key.encode("utf-8", errors="ignore")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    saturation = 150 + digest[2] % 70
    value = 135 + digest[3] % 70
    return QColor.fromHsv(hue, saturation, value)


def format_range_datetime(value, span_seconds):
    """Return a compact label for a timestamp in the visible span."""
    if span_seconds <= 24 * 60 * 60:
        return value.strftime("%H:%M")
    if span_seconds <= 14 * 24 * 60 * 60:
        return value.strftime("%d %b %H:%M")
    if span_seconds <= 180 * 24 * 60 * 60:
        return value.strftime("%d %b")
    return value.strftime("%b %Y")


def format_duration(seconds):
    """Return a human friendly duration label."""
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


class TimelineBarChart(QWidget):
    timeFilterChanged = Signal(object, object)

    def __init__(self, parent=None):
        """Initialize the instance and its default state."""
        super().__init__(parent)
        self.timeline_items = []
        self.overlay_datetimes = []
        self.overlay_label = ""
        self.end = datetime.now()
        self.filter_start = None
        self.filter_end = None
        self.drag_start = None
        self.drag_current = None
        self.hover_buckets = []
        self.hover_overlay_counts = []
        self.hover_chart_rect = QRectF()
        self.setMinimumHeight(72)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("Drag to select a time window. Mouse wheel zooms around the cursor. Double-click clears the time filter.")

    def set_timestamps(self, values, end=None):
        """Set timestamps."""
        items = []
        for value in values:
            parsed = normalize_message_datetime(value)
            if parsed is None:
                continue
            items.append((parsed, "Mailbox"))
        self.end = end or max((value for value, _folder in items), default=datetime.now())
        if end is not None:
            items = [(value, folder) for value, folder in items if value <= self.end]
        self.timeline_items = items
        self._clamp_filter_to_data()
        self.update()

    def set_timeline_items(self, items, end=None):
        """Set timeline timestamps with grouping labels used for stacked colors."""
        parsed_items = []
        for item in items:
            if isinstance(item, dict):
                raw_value = item.get("datetime") or item.get("date") or item.get("timestamp")
                folder = item.get("folder") or item.get("folder_name") or item.get("folder_path") or "Mailbox"
            else:
                try:
                    raw_value, folder = item[:2]
                except Exception:
                    raw_value, folder = item, "Mailbox"
            parsed = normalize_message_datetime(raw_value)
            if parsed is None:
                continue
            parsed_items.append((parsed, folder_leaf(folder)))
        self.end = end or max((value for value, _folder in parsed_items), default=datetime.now())
        if end is not None:
            parsed_items = [(value, folder) for value, folder in parsed_items if value <= self.end]
        self.timeline_items = parsed_items
        self._clamp_filter_to_data()
        self.update()

    def set_overlay_timestamps(self, values, label=""):
        """Set overlay timestamps."""
        self.overlay_label = str(label or "").strip()
        self.overlay_datetimes = []
        for value in values:
            parsed = value if isinstance(value, datetime) else normalize_message_datetime(value)
            if parsed is None or parsed > self.end:
                continue
            self.overlay_datetimes.append(parsed)
        self.update()

    def clear_overlay(self):
        """Clear overlay."""
        self.overlay_datetimes = []
        self.overlay_label = ""
        self.hover_overlay_counts = []
        self.update()

    def clear_time_filter(self, emit=True):
        """Clear time filter."""
        self.filter_start = None
        self.filter_end = None
        self.drag_start = None
        self.drag_current = None
        self.update()
        if emit:
            self.timeFilterChanged.emit(None, None)

    def zoom_out(self):
        """Zoom the visible/filter window out around its midpoint."""
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return
        current_start, current_end = self._display_range()
        span = max(timedelta(seconds=1), current_end - current_start)
        new_span = min(full_end - full_start, span * 2)
        center = current_start + span / 2
        new_start = center - new_span / 2
        new_end = center + new_span / 2
        self._set_filter_window(new_start, new_end)

    def mousePressEvent(self, event):
        """Start a freeform time selection drag."""
        if event.button() == Qt.LeftButton and self._plot_rect().contains(event.position().toPoint()):
            self.drag_start = event.position().toPoint()
            self.drag_current = self.drag_start
            QToolTip.hideText()
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle timeline hover and drag feedback."""
        if self.drag_start is not None:
            self.drag_current = event.position().toPoint()
            self.update()
            return
        self._show_bucket_tooltip(event.position().toPoint(), event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        """Finish a freeform time selection drag."""
        if event.button() != Qt.LeftButton or self.drag_start is None:
            super().mouseReleaseEvent(event)
            return

        start_x = self.drag_start.x()
        end_x = event.position().toPoint().x()
        self.drag_start = None
        self.drag_current = None
        if abs(end_x - start_x) < 8:
            self.update()
            return

        start = self._time_at_x(min(start_x, end_x))
        end = self._time_at_x(max(start_x, end_x))
        if start is not None and end is not None and start < end:
            self._set_filter_window(start, end)
        self.update()

    def mouseDoubleClickEvent(self, event):
        """Clear the active timeline filter on double-click."""
        if event.button() == Qt.LeftButton:
            QToolTip.hideText()
            self.clear_time_filter()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        """Zoom the visible/filter window around the cursor."""
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return

        current_start, current_end = self._display_range()
        span = max(timedelta(seconds=1), current_end - current_start)
        factor = 0.55 if event.angleDelta().y() > 0 else 1.8
        center = self._time_at_x(round(event.position().x())) or current_start + span / 2
        full_span = max(timedelta(seconds=1), full_end - full_start)
        minimum_span = timedelta(minutes=1)
        new_span = max(minimum_span, min(full_span, span * factor))
        left_weight = (center - current_start) / span
        new_start = center - new_span * left_weight
        new_end = new_start + new_span
        self._set_filter_window(new_start, new_end)

    def leaveEvent(self, event):
        """Hide timeline hover state when the mouse leaves the widget."""
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, event):
        """Paint the timeline bar chart widget."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#F8FAFC"))

        rect = self.rect().adjusted(10, 8, -10, -8)
        chart = self._plot_rect()
        if not self.timeline_items:
            painter.setPen(QColor("#64748B"))
            painter.drawText(rect, Qt.AlignCenter, "No valid email timestamps")
            return

        bucket_groups = self._bucket_folder_counts()
        self.hover_buckets = bucket_groups
        self.hover_overlay_counts = self._overlay_bucket_counts(len(bucket_groups))
        max_count = max((sum(bucket.values()) for bucket in bucket_groups), default=1)
        total_count = sum(sum(bucket.values()) for bucket in bucket_groups)
        self.hover_chart_rect = QRectF(chart)

        self._draw_grid_and_ticks(painter, chart)
        baseline = chart.bottom()
        bucket_count = len(bucket_groups)
        gap = 2
        slot_width = max(1, chart.width() / max(1, bucket_count))
        bar_width = max(2, slot_width - gap)

        for index, groups in enumerate(bucket_groups):
            count = sum(groups.values())
            if count <= 0:
                continue
            x = chart.left() + index * slot_width + gap / 2
            stack_bottom = baseline
            for folder, folder_count in self._stacked_folders(groups):
                height = max(1, (folder_count / max_count) * chart.height())
                bar_rect = QRectF(x, stack_bottom - height, bar_width, height)
                painter.fillRect(bar_rect, folder_color(folder))
                stack_bottom -= height

        self._draw_overlay(painter, chart, max_count, slot_width)
        self._draw_drag_selection(painter, chart)
        self._draw_title(painter, rect, total_count, max_count, bucket_count)
        self._draw_tick_labels(painter, chart)

    def _draw_title(self, painter, rect, total_count, max_count, bucket_count):
        painter.setPen(QColor("#334155"))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        start, end = self._display_range()
        span_seconds = max(1, (end - start).total_seconds())
        bin_label = format_duration(span_seconds / max(1, bucket_count))
        title = f"{total_count:,} messages, {bin_label} bins, peak {max_count:,}"
        if self.filter_start is not None and self.filter_end is not None:
            title += " - filtered"
        painter.drawText(rect.left(), rect.top(), rect.width(), 14, Qt.AlignLeft | Qt.AlignVCenter, title)

    def _draw_grid_and_ticks(self, painter, chart):
        ticks = self._time_ticks(max(3, min(8, int(chart.width() // 180))))
        painter.setPen(QPen(QColor("#E2E8F0"), 1))
        for timestamp in ticks:
            x = self._x_for_time(timestamp)
            painter.drawLine(x, chart.top(), x, chart.bottom())

        painter.setPen(QPen(QColor("#CBD5E1"), 1))
        painter.drawLine(chart.left(), chart.bottom(), chart.right(), chart.bottom())

    def _draw_tick_labels(self, painter, chart):
        ticks = self._time_ticks(max(3, min(8, int(chart.width() // 180))))

        start, end = self._display_range()
        span_seconds = max(1, (end - start).total_seconds())
        painter.setPen(QColor("#475569"))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        last_right = -1
        label_top = int(chart.top() + 2)
        for timestamp in ticks:
            x = self._x_for_time(timestamp)
            label = format_range_datetime(timestamp, span_seconds)
            left = int(x - 55)
            if left <= last_right + 8:
                continue
            label_rect = QRectF(left, label_top, 110, 16)
            painter.fillRect(label_rect, QColor(248, 250, 252, 210))
            painter.drawText(label_rect, Qt.AlignCenter, label)
            last_right = left + 110

    def _draw_overlay(self, painter, chart, max_count, slot_width):
        if not self.hover_overlay_counts:
            return
        overlay_color = QColor("#F97316")
        bar_width = max(1, min(slot_width * 0.36, slot_width - 1))
        for index, count in enumerate(self.hover_overlay_counts):
            if count <= 0:
                continue
            x = chart.left() + index * slot_width + max((slot_width - bar_width) / 2, 0)
            height = max(1, (count / max_count) * chart.height())
            painter.fillRect(QRectF(x, chart.bottom() - height, bar_width, height), overlay_color)

    def _draw_drag_selection(self, painter, chart):
        if self.drag_start is None or self.drag_current is None:
            return
        left = min(self.drag_start.x(), self.drag_current.x())
        right = max(self.drag_start.x(), self.drag_current.x())
        selection = QRectF(
            max(chart.left(), left),
            chart.top(),
            min(chart.right(), right) - max(chart.left(), left),
            chart.height(),
        )
        painter.fillRect(selection, QColor(37, 99, 235, 50))
        painter.setPen(QPen(QColor("#1D4ED8"), 1))
        painter.drawRect(selection)

    def _show_bucket_tooltip(self, pos, global_pos):
        if not self.hover_chart_rect.contains(pos) or not self.hover_buckets:
            QToolTip.hideText()
            return
        index = self._bucket_index_at_x(pos.x())
        if index is None:
            QToolTip.hideText()
            return
        bucket = self.hover_buckets[index]
        count = sum(bucket.values())
        bucket_start, bucket_end = self._bucket_range(index)
        folder_lines = "\n".join(
            f"{folder}: {folder_count:,}" for folder, folder_count in self._stacked_folders(bucket)[:6]
        )
        label = (
            f"{count:,} message{'s' if count != 1 else ''}\n"
            f"{bucket_start:%Y-%m-%d %H:%M}\n"
            f"to {bucket_end:%Y-%m-%d %H:%M}"
        )
        if self.hover_overlay_counts:
            overlay_name = f" ({self.overlay_label})" if self.overlay_label else ""
            label += f"\nSelected{overlay_name}: {self.hover_overlay_counts[index]:,}"
        if folder_lines:
            label += f"\n\n{folder_lines}"
        QToolTip.showText(global_pos, label, self)

    def _bucket_folder_counts(self):
        start, end = self._display_range()
        span = max(timedelta(seconds=1), end - start)
        bucket_count = self._bucket_count()
        buckets = [Counter() for _ in range(bucket_count)]
        for value, folder in self.timeline_items:
            if value < start or value > end:
                continue
            index = min(bucket_count - 1, int(((value - start) / span) * bucket_count))
            buckets[index][folder_leaf(folder)] += 1
        return buckets

    def _overlay_bucket_counts(self, bucket_count):
        if bucket_count <= 0:
            return []
        start, end = self._display_range()
        span = max(timedelta(seconds=1), end - start)
        buckets = [0 for _ in range(bucket_count)]
        for value in self.overlay_datetimes:
            if value < start or value > end:
                continue
            index = min(bucket_count - 1, int(((value - start) / span) * bucket_count))
            buckets[index] += 1
        return buckets

    def _bucket_count(self):
        width = max(1, self._plot_rect().width())
        return max(16, min(180, int(width // 10)))

    def _plot_rect(self):
        return self.rect().adjusted(14, 22, -14, -8)

    def _display_range(self):
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            now = datetime.now()
            return now - timedelta(seconds=1), now
        if self.filter_start is None or self.filter_end is None:
            return full_start, full_end
        return self.filter_start, self.filter_end

    def _full_range(self):
        values = [value for value, _folder in self.timeline_items if value is not None]
        if not values:
            return None, None
        start = min(values)
        end = max(max(values), self.end)
        if start == end:
            end = start + timedelta(seconds=1)
        return start, end

    def _time_at_x(self, x):
        chart = self._plot_rect()
        if chart.width() <= 0:
            return None
        start, end = self._display_range()
        ratio = (min(max(x, chart.left()), chart.right()) - chart.left()) / chart.width()
        return start + (end - start) * ratio

    def _x_for_time(self, value):
        chart = self._plot_rect()
        start, end = self._display_range()
        span = max(timedelta(seconds=1), end - start)
        ratio = (value - start) / span
        return int(chart.left() + ratio * chart.width())

    def _time_ticks(self, target_count):
        start, end = self._display_range()
        span_seconds = max(1, (end - start).total_seconds())
        step_seconds = self._nice_step(span_seconds / max(1, target_count - 1))
        epoch = datetime(1970, 1, 1)
        start_seconds = (start - epoch).total_seconds()
        first_seconds = ceil(start_seconds / step_seconds) * step_seconds
        ticks = []
        current = epoch + timedelta(seconds=first_seconds)
        while current <= end and len(ticks) < 20:
            ticks.append(current)
            current += timedelta(seconds=step_seconds)
        return ticks

    def _nice_step(self, raw_seconds):
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

    def _bucket_index_at_x(self, x):
        chart = self._plot_rect()
        if chart.width() <= 0 or not self.hover_buckets:
            return None
        ratio = (min(max(x, chart.left()), chart.right()) - chart.left()) / chart.width()
        return min(len(self.hover_buckets) - 1, max(0, int(ratio * len(self.hover_buckets))))

    def _bucket_range(self, index):
        start, end = self._display_range()
        span = max(timedelta(seconds=1), end - start)
        bucket_span = span / max(1, len(self.hover_buckets))
        bucket_start = start + bucket_span * index
        bucket_end = min(end, bucket_start + bucket_span)
        return bucket_start, bucket_end

    def _stacked_folders(self, groups):
        return sorted(groups.items(), key=lambda item: (-item[1], item[0]))

    def _set_filter_window(self, start, end):
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            return
        span = end - start
        if start < full_start:
            end += full_start - start
            start = full_start
        if end > full_end:
            start -= end - full_end
            end = full_end
        start = max(full_start, start)
        end = min(full_end, end)
        if start <= full_start and end >= full_end:
            self.clear_time_filter()
            return
        if end <= start:
            end = min(full_end, start + max(timedelta(seconds=1), span))
        self.filter_start = start
        self.filter_end = end
        self.update()
        self.timeFilterChanged.emit(start, end)

    def _clamp_filter_to_data(self):
        if self.filter_start is None or self.filter_end is None:
            return
        full_start, full_end = self._full_range()
        if full_start is None or full_end is None:
            self.filter_start = None
            self.filter_end = None
            return
        if self.filter_end < full_start or self.filter_start > full_end:
            self.filter_start = None
            self.filter_end = None
            return
        self.filter_start = max(full_start, self.filter_start)
        self.filter_end = min(full_end, self.filter_end)
