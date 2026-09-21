from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QToolTip, QVBoxLayout, QWidget


@dataclass
class AgendaItem:
    """Display row for a calendar or meeting-related item."""

    start: datetime | None
    end: datetime | None
    subject: str
    organizer: str = ""
    attendees: str = ""
    status: str = ""
    item_type: str = ""
    folder: str = ""
    source: str = ""
    source_ref: object = None
    details: dict | None = None


def agenda_datetime_text(value):
    """Return display text for an agenda datetime."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


class AgendaModel(QAbstractTableModel):
    HEADERS = ["Start", "End", "Status", "Subject", "Organizer", "Attendees", "Type", "Folder", "Source"]
    START_COLUMN = 0
    END_COLUMN = 1
    STATUS_COLUMN = 2
    SUBJECT_COLUMN = 3
    ORGANIZER_COLUMN = 4
    ATTENDEES_COLUMN = 5
    TYPE_COLUMN = 6
    FOLDER_COLUMN = 7
    SOURCE_COLUMN = 8

    def __init__(self):
        """Initialize the model."""
        super().__init__()
        self.all_items = []
        self.items = []
        self.sort_column = self.START_COLUMN
        self.sort_order = Qt.AscendingOrder

    def set(self, items):
        """Replace all agenda rows."""
        self.beginResetModel()
        self.all_items = list(items)
        self.items = self.sorted_items(self.all_items)
        self.endResetModel()

    def set_visible(self, items):
        """Replace visible agenda rows without changing all rows."""
        self.beginResetModel()
        self.items = self.sorted_items(list(items))
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        """Sort rows by the requested column."""
        if not (0 <= column < len(self.HEADERS)):
            return
        self.sort_column = column
        self.sort_order = order
        self.beginResetModel()
        self.items = self.sorted_items(self.items)
        self.endResetModel()

    def sorted_items(self, items):
        """Return rows sorted by the current column."""
        reverse = self.sort_order == Qt.DescendingOrder
        indexed = list(enumerate(items))
        indexed.sort(key=lambda item: self.sort_key(item[1], item[0]), reverse=reverse)
        return [item for _index, item in indexed]

    def sort_key(self, item, original_index):
        """Return a stable sort key for an agenda row."""
        column = self.sort_column
        if column == self.START_COLUMN:
            return (item.start is None, item.start or datetime.max, original_index)
        if column == self.END_COLUMN:
            return (item.end is None, item.end or datetime.max, original_index)
        if column == self.STATUS_COLUMN:
            return (item.status.lower(), original_index)
        if column == self.SUBJECT_COLUMN:
            return (item.subject.lower(), original_index)
        if column == self.ORGANIZER_COLUMN:
            return (item.organizer.lower(), original_index)
        if column == self.ATTENDEES_COLUMN:
            return (item.attendees.lower(), original_index)
        if column == self.TYPE_COLUMN:
            return (item.item_type.lower(), original_index)
        if column == self.FOLDER_COLUMN:
            return (item.folder.lower(), original_index)
        if column == self.SOURCE_COLUMN:
            return (item.source.lower(), original_index)
        return ("", original_index)

    def searchable_text(self, item):
        """Return lowercase text used by agenda search."""
        return "\n".join(
            str(value)
            for value in (
                agenda_datetime_text(item.start),
                agenda_datetime_text(item.end),
                item.status,
                item.subject,
                item.organizer,
                item.attendees,
                item.item_type,
                item.folder,
                item.source,
            )
        ).lower()

    def rowCount(self, parent=QModelIndex()):
        """Return row count."""
        return len(self.items)

    def columnCount(self, parent=QModelIndex()):
        """Return column count."""
        return len(self.HEADERS)

    def headerData(self, section, orientation, role):
        """Return header text."""
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, idx, role):
        """Return display data for a cell."""
        if not idx.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None

        item = self.items[idx.row()]
        values = [
            agenda_datetime_text(item.start),
            agenda_datetime_text(item.end),
            item.status,
            item.subject,
            item.organizer,
            item.attendees,
            item.item_type,
            item.folder,
            item.source,
        ]
        value = values[idx.column()]
        if role == Qt.ToolTipRole:
            return value
        return value

    def item(self, row):
        """Return agenda item at row."""
        return self.items[row] if 0 <= row < len(self.items) else None


def week_start_for(value):
    """Return the Monday for the week containing value."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        value = datetime.now().date()
    return value - timedelta(days=value.weekday())


def item_end_or_default(item):
    """Return an agenda item end datetime suitable for drawing."""
    if item.end is not None:
        return item.end
    if item.start is not None:
        return item.start + timedelta(minutes=30)
    return None


def agenda_tooltip_text(item):
    """Return concise human-readable tooltip text for an agenda item."""
    details = item.details or {}

    def first_detail(*names):
        """Return the first non-empty matching detail value."""
        lowered = {str(key).lower(): value for key, value in details.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value not in (None, ""):
                return str(value)
        return ""

    def clean_preview(value, limit=450):
        """Return a compact preview suitable for a tooltip."""
        text = str(value or "").replace("\r", "\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = " ".join(lines)
        if len(text) > limit:
            text = text[:limit].rstrip() + "..."
        return text

    location = first_detail(
        "Location",
        "Calendar_Location",
        "Meeting Location",
        "Message_Location",
    )
    preview = clean_preview(first_detail(
        "Preview",
        "Message_Preview",
        "Body",
        "Plain Text Body",
        "Text",
        "Message Body",
    ))

    rows = [
        ("Subject", item.subject),
        ("Start", agenda_datetime_text(item.start)),
        ("End", agenda_datetime_text(item.end)),
        ("Status", item.status),
        ("Location", location),
        ("Organizer", item.organizer),
        ("Participants", item.attendees),
        ("Preview", preview),
    ]
    text = "\n".join(f"{label}: {value}" for label, value in rows if value not in (None, ""))
    return text[:1200]


class AgendaTwoWeekGrid(QWidget):
    itemClicked = Signal(object)

    DAY_COUNT = 7
    HEADER_HEIGHT = 58
    ALL_DAY_HEIGHT = 58
    TIME_WIDTH = 48
    HOUR_HEIGHT = 48
    START_HOUR = 0
    END_HOUR = 24

    def __init__(self, parent=None):
        """Initialize the week agenda grid."""
        super().__init__(parent)
        self.items = []
        self.window_start = week_start_for(datetime.now())
        self.selected_item = None
        self.event_rects = []
        self.hover_item = None
        self.setMouseTracking(True)
        canvas_height = self.HEADER_HEIGHT + self.ALL_DAY_HEIGHT + self.HOUR_HEIGHT * (self.END_HOUR - self.START_HOUR)
        self.setMinimumHeight(canvas_height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_items(self, items):
        """Set visible agenda items."""
        self.items = list(items or [])
        self.update()

    def set_window_start(self, value):
        """Set the first day shown by the grid."""
        self.window_start = week_start_for(value)
        self.update()

    def set_selected_item(self, item):
        """Set the selected agenda item."""
        self.selected_item = item
        if item and item.start:
            self.set_window_start(item.start)
        else:
            self.update()

    def date_range(self):
        """Return the visible date range."""
        return [self.window_start + timedelta(days=offset) for offset in range(self.DAY_COUNT)]

    def visible_items(self):
        """Return items that overlap the current week window."""
        start_dt = datetime.combine(self.window_start, time.min)
        end_dt = start_dt + timedelta(days=self.DAY_COUNT)
        visible = []
        for item in self.items:
            item_start = item.start
            item_end = item_end_or_default(item)
            if item_start is None:
                continue
            if item_end is None:
                item_end = item_start + timedelta(minutes=30)
            if item_start < end_dt and item_end > start_dt:
                visible.append(item)
        return visible

    def paintEvent(self, event):
        """Paint the Outlook-style week grid."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect()
        background = self.palette().color(self.backgroundRole())
        painter.fillRect(rect, background)

        self.event_rects = []
        grid_left = self.TIME_WIDTH
        grid_width = max(1, rect.width() - grid_left)
        column_width = grid_width / self.DAY_COUNT
        body_top = self.HEADER_HEIGHT + self.ALL_DAY_HEIGHT
        today = datetime.now().date()
        days = self.date_range()

        border = QColor("#1f1f1f")
        header_bg = QColor("#202020")
        day_bg = QColor("#181818")
        today_bg = QColor("#173244")
        hour_alt = QColor("#2d2b29")
        grid_pen = QPen(QColor("#090909"))
        minor_pen = QPen(QColor("#000000"))
        minor_pen.setStyle(Qt.DotLine)
        text_color = QColor("#d8d8d8")

        painter.fillRect(rect, day_bg)
        painter.fillRect(0, 0, rect.width(), self.HEADER_HEIGHT, header_bg)
        painter.fillRect(0, self.HEADER_HEIGHT, rect.width(), self.ALL_DAY_HEIGHT, QColor("#171717"))

        painter.setPen(QPen(border))
        painter.drawLine(0, self.HEADER_HEIGHT, rect.width(), self.HEADER_HEIGHT)
        painter.drawLine(0, self.HEADER_HEIGHT + self.ALL_DAY_HEIGHT, rect.width(), self.HEADER_HEIGHT + self.ALL_DAY_HEIGHT)

        header_font = QFont(painter.font())
        header_font.setBold(False)
        painter.setFont(header_font)
        for day_index, day in enumerate(days):
            x = grid_left + day_index * column_width
            day_rect = QRectF(x, 0, column_width, rect.height())
            if day == today:
                painter.fillRect(day_rect, today_bg)
            painter.setPen(grid_pen)
            painter.drawLine(int(x), 0, int(x), rect.height())
            painter.setPen(text_color)
            weekday_text = day.strftime("%A")
            date_text = day.strftime("%d").lstrip("0")
            if day_index == 0 or day.day == 1:
                date_text = day.strftime("%d %b").lstrip("0")
            painter.drawText(QRectF(x + 6, 5, column_width - 12, 24), Qt.AlignLeft | Qt.AlignVCenter, weekday_text)
            day_font = QFont(painter.font())
            day_font.setBold(day == today)
            painter.setFont(day_font)
            painter.drawText(QRectF(x + 6, 30, column_width - 12, 22), Qt.AlignLeft | Qt.AlignVCenter, date_text)
            painter.setFont(header_font)

        painter.setPen(grid_pen)
        painter.drawLine(grid_left, 0, grid_left, rect.height())
        painter.drawLine(0, body_top, rect.width(), body_top)

        for hour in range(self.START_HOUR, self.END_HOUR + 1):
            y = body_top + (hour - self.START_HOUR) * self.HOUR_HEIGHT
            if hour < self.END_HOUR and (hour - self.START_HOUR) % 2 == 0:
                painter.fillRect(QRectF(grid_left, y, grid_width, self.HOUR_HEIGHT), hour_alt)
            painter.setPen(grid_pen)
            painter.drawLine(0, int(y), rect.width(), int(y))
            if hour < self.END_HOUR:
                painter.setPen(minor_pen)
                painter.drawLine(grid_left, int(y + self.HOUR_HEIGHT / 2), rect.width(), int(y + self.HOUR_HEIGHT / 2))
            painter.setPen(text_color)
            painter.drawText(QRectF(4, y + 2, self.TIME_WIDTH - 8, 18), Qt.AlignRight | Qt.AlignVCenter, f"{hour:02d}:00")

        self.draw_items(painter, days, grid_left, column_width, body_top)

    def draw_items(self, painter, days, grid_left, column_width, body_top):
        """Draw visible agenda items as calendar blocks."""
        timed_by_day = {day: [] for day in days}
        all_day_items = []
        window_start_dt = datetime.combine(self.window_start, time.min)
        window_end_dt = window_start_dt + timedelta(days=self.DAY_COUNT)

        for item in self.visible_items():
            item_start = max(item.start, window_start_dt)
            item_end = min(item_end_or_default(item) or item_start + timedelta(minutes=30), window_end_dt)
            if (item_end - item_start) >= timedelta(hours=20) or item_start.date() != item_end.date():
                all_day_items.append(item)
            elif item_start.date() in timed_by_day:
                timed_by_day[item_start.date()].append(item)

        all_day_lanes = []
        for item in sorted(all_day_items, key=lambda value: (value.start or datetime.max, item_end_or_default(value) or datetime.max)):
            placed = False
            item_start_day = max((item.start or window_start_dt).date(), self.window_start)
            item_end_day = min((item_end_or_default(item) or window_end_dt).date(), self.window_start + timedelta(days=self.DAY_COUNT - 1))
            for lane_index, lane in enumerate(all_day_lanes):
                if all(item_end_day < used_start or item_start_day > used_end for used_start, used_end in lane):
                    lane.append((item_start_day, item_end_day))
                    self.draw_all_day_item(painter, item, item_start_day, item_end_day, lane_index, days, grid_left, column_width)
                    placed = True
                    break
            if not placed:
                all_day_lanes.append([(item_start_day, item_end_day)])
                self.draw_all_day_item(painter, item, item_start_day, item_end_day, len(all_day_lanes) - 1, days, grid_left, column_width)

        for day, items in timed_by_day.items():
            items = sorted(items, key=lambda value: (value.start or datetime.max, item_end_or_default(value) or datetime.max))
            for item, lane_index, lane_count in self.layout_timed_items(items):
                self.draw_timed_item(painter, item, days.index(day), lane_index, lane_count, grid_left, column_width, body_top)

    def layout_timed_items(self, items):
        """Return timed items with overlap lanes sized by collision clusters."""
        clusters = []
        current = []
        current_end = None

        for item in items:
            start = item.start
            end = item_end_or_default(item)
            if start is None or end is None:
                continue
            if current and current_end is not None and start >= current_end:
                clusters.append(current)
                current = []
                current_end = None
            current.append(item)
            current_end = max(current_end or end, end)
        if current:
            clusters.append(current)

        layout = []
        for cluster in clusters:
            lane_ends = []
            assigned = []
            for item in cluster:
                start = item.start
                end = item_end_or_default(item) or start + timedelta(minutes=30)
                lane_index = None
                for index, lane_end in enumerate(lane_ends):
                    if start >= lane_end:
                        lane_index = index
                        lane_ends[index] = end
                        break
                if lane_index is None:
                    lane_index = len(lane_ends)
                    lane_ends.append(end)
                assigned.append((item, lane_index))
            lane_count = max(1, len(lane_ends))
            layout.extend((item, lane_index, lane_count) for item, lane_index in assigned)
        return layout

    def draw_all_day_item(self, painter, item, start_day, end_day, lane_index, days, grid_left, column_width):
        """Draw a multi-day or all-day item."""
        if start_day not in days and end_day not in days:
            return
        first = max(0, (start_day - self.window_start).days)
        last = min(self.DAY_COUNT - 1, (end_day - self.window_start).days)
        x = grid_left + first * column_width + 2
        y = self.HEADER_HEIGHT + 4 + lane_index * 18
        if y > self.HEADER_HEIGHT + self.ALL_DAY_HEIGHT - 20:
            return
        width = (last - first + 1) * column_width - 4
        self.draw_event_rect(painter, item, QRectF(x, y, width, 16), compact=True)

    def draw_timed_item(self, painter, item, day_index, lane_index, lane_count, grid_left, column_width, body_top):
        """Draw one timed agenda item."""
        start = item.start
        end = item_end_or_default(item)
        if start is None or end is None:
            return
        start_hour = start.hour + start.minute / 60
        end_hour = end.hour + end.minute / 60
        start_hour = max(self.START_HOUR, min(self.END_HOUR, start_hour))
        end_hour = max(start_hour + 0.25, min(self.END_HOUR, end_hour))
        if start_hour >= self.END_HOUR:
            return
        y = body_top + (start_hour - self.START_HOUR) * self.HOUR_HEIGHT
        height = max(18, (end_hour - start_hour) * self.HOUR_HEIGHT - 2)
        overlap_count = max(lane_count, 1)
        lane = min(lane_index, overlap_count - 1)
        item_width = max(30, (column_width - 5) / overlap_count)
        x = grid_left + day_index * column_width + 2 + lane * item_width
        self.draw_event_rect(painter, item, QRectF(x, y + 1, item_width - 3, height))

    def draw_event_rect(self, painter, item, rect, compact=False):
        """Draw a single blue agenda item block."""
        selected = item is self.selected_item
        fill, edge = self.event_colors(item, selected)
        painter.fillRect(rect, fill)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.setPen(QColor("#ffffff"))
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSize(max(7, font.pointSize() - 1))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = item.subject or "(no subject)"
        if not compact and item.organizer:
            text = f"{text}\n{item.organizer}"
        if compact:
            text = metrics.elidedText(text.replace("\n", " "), Qt.ElideRight, int(rect.width() - 8))
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, text)
        else:
            painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
        self.event_rects.append((QRectF(rect), item))

    def event_colors(self, item, selected=False):
        """Return fill and edge colors based on attendance status."""
        status = f"{item.status} {item.item_type} {item.source}".lower()
        if "declin" in status or "refus" in status or "resp.neg" in status:
            fill = QColor("#8a2f3c")
            edge = QColor("#d96b79")
        elif "tentative" in status or "resp.tent" in status:
            fill = QColor("#8a6a15")
            edge = QColor("#e0b94e")
        elif "cancel" in status:
            fill = QColor("#5b5f66")
            edge = QColor("#aeb4bd")
        elif "accepted" in status or "resp.pos" in status:
            fill = QColor("#1f6f43")
            edge = QColor("#67c68c")
        elif "request" in status:
            fill = QColor("#005a9e")
            edge = QColor("#2b8dcc")
        else:
            fill = QColor("#4a5f78")
            edge = QColor("#85a7c7")
        if selected:
            return fill.lighter(125), QColor("#ffffff")
        return fill, edge

    def mousePressEvent(self, event):
        """Select the agenda item under the pointer."""
        item = self.item_at_event(event)
        if item is not None:
            self.selected_item = item
            self.itemClicked.emit(item)
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Show a detailed popup for the meeting under the pointer."""
        item = self.item_at_event(event)
        if item is not self.hover_item:
            self.hover_item = item
            if item is None:
                QToolTip.hideText()
            else:
                global_pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
                QToolTip.showText(global_pos, agenda_tooltip_text(item), self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """Hide item tooltip when the pointer leaves the grid."""
        self.hover_item = None
        QToolTip.hideText()
        super().leaveEvent(event)

    def item_at_event(self, event):
        """Return the item under a mouse event."""
        point = event.position() if hasattr(event, "position") else event.pos()
        for rect, item in reversed(self.event_rects):
            if rect.contains(point):
                return item
        return None


class AgendaTwoWeekView(QWidget):
    itemClicked = Signal(object)

    def __init__(self, parent=None):
        """Initialize the week agenda view."""
        super().__init__(parent)
        self.grid = AgendaTwoWeekGrid()
        self.grid.itemClicked.connect(lambda item: self.itemClicked.emit(item))
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.grid)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setMinimumHeight(140)
        self.scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.today_button = QPushButton("Today")
        self.previous_button = QPushButton("<")
        self.next_button = QPushButton(">")
        self.range_label = QLabel("")
        label_font = QFont(self.range_label.font())
        label_font.setBold(True)
        self.range_label.setFont(label_font)

        self.today_button.clicked.connect(lambda _checked=False: self.go_today())
        self.previous_button.clicked.connect(lambda _checked=False: self.shift_weeks(-1))
        self.next_button.clicked.connect(lambda _checked=False: self.shift_weeks(1))

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self.today_button)
        controls_layout.addWidget(self.previous_button)
        controls_layout.addWidget(self.next_button)
        controls_layout.addWidget(self.range_label)
        controls_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(controls)
        layout.addWidget(self.scroll_area, 1)
        self.update_range_label()

    def set_items(self, items):
        """Set agenda items visible in the calendar."""
        self.grid.set_items(items)

    def set_selected_item(self, item):
        """Select an item and move the grid to its week."""
        self.grid.set_selected_item(item)
        self.scroll_to_item(item)
        self.update_range_label()

    def go_today(self):
        """Jump to the current week."""
        self.grid.set_window_start(datetime.now())
        self.scroll_to_hour(8)
        self.update_range_label()

    def shift_weeks(self, weeks):
        """Move the visible window by a number of weeks."""
        self.grid.set_window_start(self.grid.window_start + timedelta(weeks=weeks))
        self.update_range_label()

    def scroll_to_item(self, item):
        """Scroll the hour view to keep the selected item visible."""
        if item is None or item.start is None:
            return
        self.scroll_to_hour(item.start.hour + item.start.minute / 60)

    def scroll_to_hour(self, hour):
        """Scroll to an hour in the week view."""
        body_top = self.grid.HEADER_HEIGHT + self.grid.ALL_DAY_HEIGHT
        y = body_top + max(0, hour - self.grid.START_HOUR) * self.grid.HOUR_HEIGHT
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(max(0, int(y - self.scroll_area.viewport().height() * 0.25)))

    def update_range_label(self):
        """Update toolbar range text."""
        start = self.grid.window_start
        end = start + timedelta(days=self.grid.DAY_COUNT - 1)
        if start.year == end.year:
            if start.month == end.month:
                text = f"{start.day} - {end.day} {end.strftime('%B %Y')}"
            else:
                text = f"{start.day} {start.strftime('%b')} - {end.day} {end.strftime('%b %Y')}"
        else:
            text = f"{start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')}"
        self.range_label.setText(text)
