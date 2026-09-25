"""
Stay Finder: desktop app for the Airbnb + local Ollama search in StayFinderSearch.py.

    python StayFinder.py

Set the trip and filters in the sidebar, pick a local model and hit Search (or Ctrl+Enter).
Progress streams in while it runs; results show as photo cards, and clicking a card opens its
details and photo gallery. The Markdown report is written to the reports folder and can be
exported (Ctrl+S) or copied. Follows the Windows light/dark setting and remembers your last search.

Requires: pip install -r requirements.txt
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from string import Template

import ollama
from PySide6.QtCore import (Property, QDate, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QRectF,
                            QSettings, QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication, QKeySequence, QPainter, QPainterPath,
                           QPalette, QPixmap, QShortcut)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFrame,
                               QHBoxLayout,
                               QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)
from shiboken6 import isValid

import StayFinderSearch as engine

# "Dryer" is left out on purpose: whole-word matching would accept "Hair dryer"
COMMON_AMENITIES = ["Wifi", "Kitchen", "Washer", "Air conditioning", "Free parking", "Pool", "Hot tub",
                    "Beach access", "Pets allowed", "Dedicated workspace", "EV charger", "Fireplace", "Crib"]
PLACE_TYPES = {"Any": "", "Entire place": "Entire home/apt", "Private room": "Private room"}
RATINGS = {"Any": 0.0, "4.5+": 4.5, "4.7+": 4.7, "4.8+": 4.8, "4.9+": 4.9}
CARD_WIDTH = 300

LIGHT = dict(bg="#F5F6FA", surface="#FFFFFF", surface2="#F0F1F6", hover="#E7E9F1", border="#E2E4EC",
             text="#161922", muted="#6A7086", accent="#5B5BD6", accent_hover="#4B4BC6", accent_soft="#ECECFE",
             on_accent="#FFFFFF", good="#16A34A", bad="#E5484D", overlay="rgba(15, 17, 23, 0.72)")
DARK = dict(bg="#0E1015", surface="#161922", surface2="#1E222D", hover="#262B38", border="#2A2F3C",
            text="#E9EBF2", muted="#8F96AB", accent="#7B7BEF", accent_hover="#8E8EF4", accent_soft="#23264A",
            on_accent="#FFFFFF", good="#3DD68C", bad="#FF6B6F", overlay="rgba(10, 12, 16, 0.78)")
THEME = LIGHT

STYLESHEET = Template("""
* { font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; color: $text; }
#Root, #Page, QScrollArea, QScrollArea > QWidget > QWidget { background: $bg; }
#Sidebar, #Sidebar QScrollArea, #Sidebar QScrollArea > QWidget > QWidget { background: $surface; }
#Sidebar { border-right: 1px solid $border; }
#SidebarFooter { background: $surface; border-top: 1px solid $border; }

#Card { background: $surface; border: 1px solid $border; border-radius: 14px; }
#Sidebar #Card { background: $surface; border: none; border-bottom: 1px solid $border; border-radius: 0; }
#ListingCard { background: $surface; border: 1px solid $border; border-radius: 16px; }
#ListingCard:hover { border: 1px solid $accent; }

QLabel { background: transparent; }
QLabel#AppTitle { font-size: 15pt; font-weight: 700; }
QLabel#H1 { font-size: 22pt; font-weight: 700; }
QLabel#H2 { font-size: 14pt; font-weight: 650; }
QLabel#H3 { font-size: 11.5pt; font-weight: 650; }
QLabel#Section { font-size: 8.5pt; font-weight: 700; color: $muted; }
QLabel#Muted { color: $muted; }
QLabel#Price { font-size: 11.5pt; font-weight: 700; }
QLabel#Error { color: $bad; font-weight: 600; }
QLabel#Badge { background: $accent_soft; color: $accent; border-radius: 9px; padding: 2px 9px;
               font-size: 8.5pt; font-weight: 600; }
QLabel#Tag { background: $surface2; border: 1px solid $border; border-radius: 12px; padding: 4px 10px; }
QLabel#Rank { background: $overlay; color: #FFFFFF; border-radius: 11px; padding: 3px 10px; font-weight: 700; }
QLabel#Emoji { font-size: 44pt; }

QLineEdit, QPlainTextEdit, QSpinBox, QDateEdit, QComboBox {
    background: $surface2; border: 1px solid $border; border-radius: 10px; padding: 7px 10px;
    selection-background-color: $accent; selection-color: $on_accent; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDateEdit:focus, QComboBox:focus { border: 1px solid $accent; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; }
QDateEdit::drop-down, QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView { background: $surface; border: 1px solid $border; border-radius: 8px; padding: 4px;
    outline: 0; selection-background-color: $accent_soft; selection-color: $text; }

QPushButton { background: $surface2; border: 1px solid $border; border-radius: 10px; padding: 8px 16px;
              font-weight: 600; }
QPushButton:hover { background: $hover; }
QPushButton:disabled { color: $muted; }
QPushButton#Primary { background: $accent; color: $on_accent; border: none; padding: 12px 18px; font-size: 11pt; }
QPushButton#Primary:hover { background: $accent_hover; }
QPushButton#Primary:disabled { background: $accent_soft; color: $muted; }
QPushButton#Ghost { background: transparent; border: none; color: $accent; padding: 6px 4px; }
QPushButton#Ghost:hover { color: $accent_hover; }
QPushButton#Chip { background: $surface2; border: 1px solid $border; border-radius: 15px; padding: 5px 13px;
                   font-weight: 500; }
QPushButton#Chip:hover { border: 1px solid $muted; }
QPushButton#Chip:checked { background: $accent_soft; border: 1px solid $accent; color: $accent; font-weight: 600; }
QPushButton#Round { border-radius: 15px; padding: 0; min-width: 30px; max-width: 30px; min-height: 30px;
                    max-height: 30px; font-size: 13pt; font-weight: 500; }
#Segmented { background: $surface2; border: 1px solid $border; border-radius: 11px; }
QPushButton#Segment { background: transparent; border: none; border-radius: 8px; padding: 6px 10px;
                      color: $muted; font-weight: 600; }
QPushButton#Segment:hover { color: $text; }
QPushButton#Segment:checked { background: $surface; color: $text; border: 1px solid $border; }

QProgressBar { background: $surface2; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: $accent; border-radius: 3px; }
QListWidget { background: transparent; border: none; outline: 0; }
QListWidget::item { padding: 7px 4px; border: none; }
QListWidget::item:selected { background: transparent; color: $text; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: $border; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 3px; }
QScrollBar::handle:horizontal { background: $border; border-radius: 4px; min-width: 36px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QCalendarWidget QWidget { alternate-background-color: $surface2; background: $surface; }
QCalendarWidget QToolButton { background: transparent; border: none; border-radius: 6px; padding: 4px 8px;
                              font-weight: 600; }
QCalendarWidget QToolButton:hover { background: $hover; }
QCalendarWidget QAbstractItemView { selection-background-color: $accent; selection-color: $on_accent; outline: 0; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 5px; }
""")


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------
def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def hbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            layout.addStretch(1)
        elif isinstance(w, QLayout):
            layout.addLayout(w)
        else:
            layout.addWidget(w)
    return layout


def vbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            layout.addStretch(1)
        elif isinstance(w, QLayout):
            layout.addLayout(w)
        else:
            layout.addWidget(w)
    return layout


def clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def sized_image(url: str, width: int) -> str:
    # Airbnb's CDN only serves certain widths (e.g. 640 and 1600 are 404s), so round up to one it has
    allowed = (240, 320, 480, 720, 960, 1200, 1440, 1920)
    return f"{url.split('?')[0]}?im_w={next((w for w in allowed if w >= width), allowed[-1])}"


def open_path(path: Path) -> None:
    try:
        os.startfile(path)
    except OSError:                     # no app associated with .md
        subprocess.Popen(["notepad.exe", str(path)])


class FlowLayout(QLayout):
    """Lays children out left to right, wrapping to new rows (chips, cards)."""

    def __init__(self, parent=None, spacing: int = 8):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, apply: bool) -> int:
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_height:
                x, y, row_height = rect.x(), y + row_height + self._spacing, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y()


class Switch(QCheckBox):
    """iOS/Win11-style toggle with a sliding knob."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)
        self._knob = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool):
        self._anim.stop()
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def get_knob(self):
        return self._knob

    def set_knob(self, value):
        self._knob = value
        self.update()

    knob = Property(float, get_knob, set_knob)

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        self._knob = 1.0 if checked else 0.0

    def sizeHint(self):
        return QSize(46 + self.fontMetrics().horizontalAdvance(self.text()) + 4, 26)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0, (self.height() - 22) / 2, 40, 22)
        off, on = QColor(THEME["border"]), QColor(THEME["accent"])
        color = QColor(int(off.red() + (on.red() - off.red()) * self._knob),
                       int(off.green() + (on.green() - off.green()) * self._knob),
                       int(off.blue() + (on.blue() - off.blue()) * self._knob))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(track, 11, 11)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(track.x() + 3 + self._knob * 18, track.y() + 3, 16, 16))
        p.setPen(QColor(THEME["text"]))
        p.drawText(QRect(50, 0, self.width() - 50, self.height()), Qt.AlignVCenter, self.text())


class Stepper(QWidget):
    """  –  3  +   number picker, like a booking site's guest selector."""

    def __init__(self, value: int, minimum: int = 0, maximum: int = 20, zero_text: str | None = None):
        super().__init__()
        self.minimum, self.maximum, self.zero_text = minimum, maximum, zero_text
        self.minus, self.plus = QPushButton("−"), QPushButton("+")
        self.display = label("")
        self.display.setAlignment(Qt.AlignCenter)
        self.display.setMinimumWidth(40)
        for button, delta in ((self.minus, -1), (self.plus, 1)):
            button.setObjectName("Round")
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _, d=delta: self.setValue(self._value + d))
        self.setLayout(hbox(self.minus, self.display, self.plus, spacing=6))
        self._value = minimum
        self.setValue(value)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int):
        self._value = max(self.minimum, min(self.maximum, int(value)))
        self.display.setText(self.zero_text if self._value == 0 and self.zero_text else str(self._value))
        self.minus.setEnabled(self._value > self.minimum)
        self.plus.setEnabled(self._value < self.maximum)


class Segmented(QFrame):
    """A row of mutually exclusive options in a pill."""

    def __init__(self, options: list[str], current: str):
        super().__init__()
        self.setObjectName("Segmented")
        self.group = QButtonGroup(self)
        layout = hbox(spacing=2, margins=(3, 3, 3, 3))
        for option in options:
            button = QPushButton(option)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(option == current)
            self.group.addButton(button)
            layout.addWidget(button)
        self.setLayout(layout)

    def value(self) -> str:
        return self.group.checkedButton().text()


class ImageLoader(QObject):
    """Async image downloads with an in-memory cache."""

    def __init__(self):
        super().__init__()
        self.manager = QNetworkAccessManager(self)
        self.cache: dict[str, QPixmap] = {}
        self.waiting: dict[str, list] = {}

    def load(self, url: str, callback) -> None:
        if url in self.cache:
            callback(self.cache[url])
            return
        if url in self.waiting:
            self.waiting[url].append(callback)
            return
        self.waiting[url] = [callback]
        reply = self.manager.get(QNetworkRequest(QUrl(url)))
        reply.finished.connect(lambda r=reply, u=url: self._finished(u, r))

    def _finished(self, url: str, reply: QNetworkReply) -> None:
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(reply.readAll())
        reply.deleteLater()
        if not pixmap.isNull():
            self.cache[url] = pixmap
        for callback in self.waiting.pop(url, []):
            callback(pixmap)


IMAGES = None                           # ImageLoader, created once the QApplication exists


class Photo(QWidget):
    """Rounded, center-cropped photo that loads from a URL."""
    clicked = Signal()

    def __init__(self, width: int | None, height: int, radius: int = 12, top_only: bool = False):
        """width=None stretches to the available width."""
        super().__init__()
        if width:
            self.setFixedSize(width, height)
        else:
            self.setFixedHeight(height)
            self.setMinimumWidth(320)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.radius, self.top_only = radius, top_only
        self.pixmap: QPixmap | None = None
        self.selected = False

    def set_url(self, url: str | None, width: int) -> None:
        self.pixmap = None
        self.update()
        if url:
            IMAGES.load(sized_image(url, width), self._loaded)

    def _loaded(self, pixmap: QPixmap) -> None:
        if isValid(self) and not pixmap.isNull():
            self.pixmap = pixmap
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        if self.top_only:                   # square off the bottom corners
            path.setFillRule(Qt.WindingFill)
            path.addRect(QRectF(0, rect.height() / 2, rect.width(), rect.height() / 2))
            path = path.simplified()
        p.setClipPath(path)
        if self.pixmap:
            scaled = self.pixmap.scaled(self.size() * self.devicePixelRatioF(), Qt.KeepAspectRatioByExpanding,
                                        Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(self.devicePixelRatioF())
            logical = scaled.deviceIndependentSize()
            p.drawPixmap(QPoint(int((self.width() - logical.width()) / 2),
                                int((self.height() - logical.height()) / 2)), scaled)
        else:
            p.fillRect(rect, QColor(THEME["surface2"]))
        if self.selected:
            p.setClipping(False)
            p.setPen(QColor(THEME["accent"]))
            p.setBrush(Qt.NoBrush)
            pen = p.pen()
            pen.setWidth(3)
            p.setPen(pen)
            p.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), self.radius, self.radius)


def card(*children, spacing: int = 10, margins=(18, 16, 18, 18), name: str = "Card") -> QFrame:
    frame = QFrame()
    frame.setObjectName(name)
    frame.setLayout(vbox(*children, spacing=spacing, margins=margins))
    return frame


def badges_for(record: dict) -> list[str]:
    return [b for b, on in (("Superhost", record["superhost"]), ("Guest favorite", record["guest_favorite"]),
                            ("Free cancellation", record["free_cancellation"])) if on]


def rating_text(record: dict) -> str:
    return f"★ {record['rating']:.2f}  ·  {record['reviews']} reviews" if record["reviews"] else "★ New listing"


def flow_of(texts: list[str], name: str, spacing: int = 6) -> QWidget:
    holder = QWidget()
    flow = FlowLayout(holder, spacing=spacing)
    for text in texts:
        flow.addWidget(label(text, name))
    return holder


# ---------------------------------------------------------------------------
# Listing card (results grid)
# ---------------------------------------------------------------------------
class ListingCard(QFrame):
    clicked = Signal(dict)

    def __init__(self, record: dict):
        super().__init__()
        self.record = record
        self.setObjectName("ListingCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(CARD_WIDTH)

        photo = Photo(CARD_WIDTH - 2, 196, radius=15, top_only=True)
        photo.set_url(record["cover_image"], 640)
        if "rank" in record:
            rank = label(f"#{record['rank']}", "Rank", )
            rank.setParent(photo)
            rank.move(12, 12)
            rank.adjustSize()

        title = label(record.get("headline") or record["property_type"], "H3", wrap=True)
        name = label(self.fontMetrics().elidedText(record["name"], Qt.ElideRight, CARD_WIDTH - 36), "Muted")
        name.setToolTip(record["name"])
        price = label(f"${record['price_per_night']:,} night", "Price")
        total = label(f"${record['total_price']:,} total", "Muted")
        rating = label(rating_text(record))
        layout = label(record["layout"], "Muted", wrap=True)

        body = vbox(title, name, hbox(price, total, None, spacing=8), rating, layout, spacing=4,
                    margins=(16, 12, 16, 16))
        if badges_for(record):
            body.addSpacing(4)
            body.addWidget(flow_of(badges_for(record), "Badge"))
        outer = vbox(photo, spacing=0, margins=(1, 1, 1, 1))
        outer.addLayout(body)
        self.setLayout(outer)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.record)


# ---------------------------------------------------------------------------
# Sidebar (search form)
# ---------------------------------------------------------------------------
class Sidebar(QFrame):
    search_requested = Signal()

    def __init__(self, saved: dict):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(410)
        c = engine.CRITERIA
        s = {**asdict(c), **saved.get("criteria", {})}

        logo = label("🏝")
        logo.setStyleSheet("font-size: 22pt;")
        header = hbox(logo, vbox(label("Stay Finder", "AppTitle"),
                                 label("Airbnb search with a local model", "Muted"), spacing=0),
                      None, spacing=12, margins=(22, 18, 22, 14))

        # Where & when
        self.location = QLineEdit(s["location"])
        self.location.setPlaceholderText("City, neighborhood or area")
        self.check_in, self.check_out = self._date_edit(s["check_in"]), self._date_edit(s["check_out"])
        self.nights = label("", "Muted")
        for edit in (self.check_in, self.check_out):
            edit.dateChanged.connect(self._update_nights)
        where = self._section("WHERE & WHEN", self.location,
                              hbox(self._field("Check-in", self.check_in), self._field("Check-out", self.check_out),
                                   spacing=10),
                              self.nights)
        self._update_nights()

        # Guests
        self.adults = Stepper(s["adults"], 1, 16)
        self.children = Stepper(s["children"], 0, 16)
        self.infants = Stepper(s["infants"], 0, 5)
        guests = self._section("GUESTS", self._row("Adults", self.adults, "Ages 13+"),
                               self._row("Children", self.children, "Ages 2–12"),
                               self._row("Infants", self.infants, "Under 2"))

        # Property
        self.place_type = Segmented(list(PLACE_TYPES),
                                    next((k for k, v in PLACE_TYPES.items() if v == s["place_type"]), "Any"))
        self.bedrooms = Stepper(s["min_bedrooms"], 0, 20, "Any")
        self.beds = Stepper(s["min_beds"], 0, 30, "Any")
        self.baths = Stepper(s["min_bathrooms"], 0, 20, "Any")
        prop = self._section("PROPERTY", self.place_type, self._row("Bedrooms", self.bedrooms),
                             self._row("Beds", self.beds), self._row("Bathrooms", self.baths))

        # Budget & quality
        self.min_price = self._money(s["min_price_per_night"], "No min")
        self.max_price = self._money(s["max_price_per_night"], "No max")
        rating = next((k for k, v in sorted(RATINGS.items(), key=lambda kv: -kv[1]) if v <= s["min_rating"]), "Any")
        self.min_rating = Segmented(list(RATINGS), rating)
        self.min_reviews = QSpinBox()
        self.min_reviews.setRange(0, 5000)
        self.min_reviews.setSingleStep(5)
        self.min_reviews.setSpecialValueText("Any")
        self.min_reviews.setValue(s["min_reviews"])
        budget = self._section(
            "BUDGET & QUALITY",
            hbox(self._field("Min per night", self.min_price), self._field("Max per night", self.max_price),
                 spacing=10),
            label("All-in price: Airbnb's total ÷ nights", "Muted"),
            self._field("Guest rating", self.min_rating),
            self._row("Minimum reviews", self.min_reviews))

        # Must-haves
        chosen = {a.lower() for a in s["required_amenities"]}
        chips_holder = QWidget()
        chips = FlowLayout(chips_holder, spacing=7)
        self.amenity_chips = {}
        for amenity in COMMON_AMENITIES:
            chip = QPushButton(amenity)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setChecked(amenity.lower() in chosen)
            chips.addWidget(chip)
            self.amenity_chips[amenity] = chip
        others = [a for a in s["required_amenities"] if a.lower() not in {x.lower() for x in COMMON_AMENITIES}]
        self.other_amenities = QLineEdit(", ".join(others))
        self.other_amenities.setPlaceholderText("Other must-haves, comma-separated")
        self.superhost = Switch("Superhost only")
        self.superhost.setChecked(s["superhost_only"])
        self.guest_favorite = Switch("Guest favorites only")
        self.guest_favorite.setChecked(s["guest_favorite_only"])
        self.free_cancel = Switch("Free cancellation")
        self.free_cancel.setChecked(s["free_cancellation"])
        musts = self._section("MUST-HAVES", chips_holder, self.other_amenities, self.superhost,
                              self.guest_favorite, self.free_cancel)

        # Preferences
        self.preferences = QPlainTextEdit(s["preferences"])
        self.preferences.setPlaceholderText("e.g. Walkable to the beach, quiet street, great kitchen for cooking")
        self.preferences.setFixedHeight(92)
        prefs = self._section("WHAT MATTERS MOST", label("The model ranks listings against this.", "Muted"),
                              self.preferences)

        # Model
        self.model = QComboBox()
        self.model.setEditable(False)
        self.model.addItem(saved.get("model") or engine.MODEL)
        self.think = Switch("Thinking (smarter, much slower)")
        self.think.setChecked(bool(saved.get("think", bool(engine.THINK))))
        self.picks = Stepper(s["max_results"], 1, 20)
        model = self._section("MODEL", self.model, self.think, self._row("Number of picks", self.picks))

        form = QWidget()
        form.setLayout(vbox(where, guests, prop, budget, musts, prefs, model, None, spacing=0))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(form)

        self.error = label("", "Error", wrap=True)
        self.error.hide()
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("Primary")
        self.search_button.setCursor(Qt.PointingHandCursor)
        self.search_button.clicked.connect(self.search_requested.emit)
        shortcut_hint = label("or press Ctrl+Enter", "Muted")
        shortcut_hint.setAlignment(Qt.AlignCenter)
        footer = QFrame()
        footer.setObjectName("SidebarFooter")
        footer.setLayout(vbox(self.error, self.search_button, shortcut_hint, spacing=8, margins=(22, 14, 22, 14)))

        top = QWidget()
        top.setLayout(header)
        top.setObjectName("SidebarHeader")
        self.setLayout(vbox(top, scroll, footer, spacing=0))

    # -- helpers --
    def _section(self, title: str, *children) -> QFrame:
        return card(label(title, "Section"), *children, spacing=10, margins=(22, 16, 22, 18))

    def _field(self, title: str, widget) -> QWidget:
        holder = QWidget()
        holder.setLayout(vbox(label(title, "Muted"), widget, spacing=4))
        return holder

    def _row(self, title: str, widget, hint: str | None = None) -> QWidget:
        holder = QWidget()
        text = vbox(label(title), spacing=0)
        if hint:
            text.addWidget(label(hint, "Muted"))
        holder.setLayout(hbox(text, None, widget))
        return holder

    def _date_edit(self, iso: str) -> QDateEdit:
        edit = QDateEdit(QDate.fromString(iso, "yyyy-MM-dd"))
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("ddd, MMM d, yyyy")
        edit.setMinimumDate(QDate.currentDate())
        return edit

    def _money(self, value: int, zero_text: str) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 20000)
        box.setSingleStep(25)
        box.setPrefix("$")
        box.setSpecialValueText(zero_text)
        box.setValue(value)
        return box

    def _update_nights(self):
        nights = self.check_in.date().daysTo(self.check_out.date())
        self.nights.setText(f"{nights} night{'s' if nights != 1 else ''}" if nights > 0
                            else "Check-out must be after check-in")

    # -- data --
    def set_models(self, names: list[str]) -> None:
        current = self.model.currentText()
        self.model.clear()
        self.model.addItems(names)
        self.model.setCurrentText(current if current in names else names[0])

    def show_error(self, message: str | None) -> None:
        self.error.setText(message or "")
        self.error.setVisible(bool(message))

    def criteria(self) -> engine.SearchCriteria:
        location = self.location.text().strip()
        if not location:
            raise ValueError("Enter a location.")
        check_in, check_out = self.check_in.date(), self.check_out.date()
        if check_in < QDate.currentDate():
            raise ValueError("Check-in is in the past.")
        if check_out <= check_in:
            raise ValueError("Check-out must be after check-in.")
        if self.max_price.value() and self.max_price.value() < self.min_price.value():
            raise ValueError("Max price is below min price.")
        amenities = [name for name, chip in self.amenity_chips.items() if chip.isChecked()]
        amenities += [a.strip() for a in self.other_amenities.text().split(",") if a.strip()]
        return engine.SearchCriteria(
            location=location,
            check_in=check_in.toString("yyyy-MM-dd"),
            check_out=check_out.toString("yyyy-MM-dd"),
            adults=self.adults.value(),
            children=self.children.value(),
            infants=self.infants.value(),
            min_bedrooms=self.bedrooms.value(),
            min_beds=self.beds.value(),
            min_bathrooms=self.baths.value(),
            min_price_per_night=self.min_price.value(),
            max_price_per_night=self.max_price.value(),
            place_type=PLACE_TYPES[self.place_type.value()],
            min_rating=RATINGS[self.min_rating.value()],
            min_reviews=self.min_reviews.value(),
            required_amenities=list(dict.fromkeys(amenities)),
            superhost_only=self.superhost.isChecked(),
            guest_favorite_only=self.guest_favorite.isChecked(),
            free_cancellation=self.free_cancel.isChecked(),
            preferences=self.preferences.toPlainText().strip(),
            max_results=self.picks.value(),
            currency=engine.CRITERIA.currency,
        )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
class EmptyPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        emoji = label("🏡", "Emoji")
        emoji.setAlignment(Qt.AlignCenter)
        title = label("Find your stay", "H1")
        title.setAlignment(Qt.AlignCenter)
        text = label("Set your trip on the left and hit Search. A model running on this PC searches Airbnb, "
                     "checks the most promising listings and ranks them against what matters to you.",
                     "Muted", wrap=True)
        text.setAlignment(Qt.AlignCenter)
        text.setMaximumWidth(460)
        self.setLayout(vbox(None, emoji, title, hbox(None, text, None), None, spacing=12))


class RunningPage(QWidget):
    stop_requested = Signal()
    back_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.title = label("", "H1", wrap=True)
        self.subtitle = label("", "Muted", wrap=True)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.status = label("", "Muted")
        self.elapsed = label("", "Muted")
        self.steps = QListWidget()
        self.steps.setFocusPolicy(Qt.NoFocus)
        self.steps.setSelectionMode(QListWidget.NoSelection)
        self.steps.setWordWrap(True)
        self.steps.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setStyleSheet("font-family: Consolas; font-size: 9pt;")
        self.raw.hide()
        self.toggle_raw = QPushButton("Show full log")
        self.toggle_raw.setObjectName("Ghost")
        self.toggle_raw.clicked.connect(self._toggle_raw)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.back_button.hide()

        steps_card = card(label("PROGRESS", "Section"), self.steps, hbox(self.toggle_raw, None), self.raw,
                          spacing=8)
        content = QWidget()
        content.setMaximumWidth(820)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        content.setLayout(vbox(self.title, self.subtitle, self.progress, hbox(self.status, None, self.elapsed),
                               steps_card,
                               hbox(self.stop_button, self.back_button, None), spacing=12,
                               margins=(40, 40, 40, 32)))
        outer = hbox(None, content, None, spacing=0)
        outer.setStretch(1, 20)             # content takes the space up to its max width; spacers center it
        self.setLayout(outer)
        self.details_item: QListWidgetItem | None = None
        self.details_count = 0

    def _toggle_raw(self):
        self.raw.setVisible(not self.raw.isVisible())
        self.toggle_raw.setText("Hide full log" if self.raw.isVisible() else "Show full log")

    def start(self, criteria: engine.SearchCriteria, model: str):
        nights = QDate.fromString(criteria.check_in, "yyyy-MM-dd").daysTo(
            QDate.fromString(criteria.check_out, "yyyy-MM-dd"))
        self.title.setText(f"Searching {criteria.location}")
        guests = criteria.adults + criteria.children
        self.subtitle.setText(f"{criteria.check_in} → {criteria.check_out}  ·  {nights} nights  ·  "
                              f"{guests} guest{'s' if guests != 1 else ''}  ·  {model}")
        self.progress.setRange(0, 0)
        self.status.setText("Starting…")
        self.elapsed.setText("0:00")
        self.steps.clear()
        self.raw.clear()
        self.details_item, self.details_count = None, 0
        self.stop_button.show()
        self.stop_button.setEnabled(True)
        self.back_button.hide()

    def add_step(self, text: str, color: str | None = None):
        item = QListWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        self.steps.addItem(item)
        self.steps.scrollToBottom()
        return item

    def log(self, line: str):
        """Turn the engine's log lines into friendly progress steps."""
        self.raw.appendPlainText(line)
        s = line.strip()
        if match := re.match(r"\[turn (\d+)\]", s):
            self.status.setText(f"Model is thinking · step {match.group(1)}")
        elif match := re.match(r"-> search_listings\((.*)\)", s):
            where = re.search(r"location='([^']*)'", match.group(1))
            radius = re.search(r"radius_km=([\d.]+)", match.group(1))
            self.add_step(f"🔎   Searching Airbnb in {where.group(1) if where else 'the area'}"
                          f"{f' ({radius.group(1)} km radius)' if radius else ''}")
            self.details_item = None
        elif match := re.match(r"(\d+) found, (\d+) passed", s):
            self.add_step(f"✓   Found {match.group(1)} listings · {match.group(2)} match your filters",
                          THEME["good"])
        elif s.startswith("-> get_listing_details"):
            self.details_count += 1
            text = f"🏠   Checked {self.details_count} listing{'s' if self.details_count != 1 else ''} in detail"
            if self.details_item is None:
                self.details_item = self.add_step(text)
            else:
                self.details_item.setText(text)
        elif s.startswith("(asking the model"):
            self.add_step("↻   Asking the model to double-check its shortlist")
        elif s.startswith("[final]"):
            self.add_step("✍   Writing up the picks")
            self.status.setText("Writing up the picks…")
        elif s.startswith("!"):
            self.add_step(f"⚠   {s.lstrip('! ').strip()}", THEME["bad"])

    def set_elapsed(self, text: str):
        self.elapsed.setText(text)

    def stopping(self):
        self.stop_button.setEnabled(False)
        self.status.setText("Stopping after the current step…")

    def finished_badly(self, title: str, message: str):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.title.setText(title)
        self.status.setText(message)
        self.stop_button.hide()
        self.back_button.show()


class ResultsPage(QScrollArea):
    open_record = Signal(dict)
    open_report = Signal()
    export_report = Signal()
    copy_report = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.title = label("", "H1", wrap=True)
        self.subtitle = label("", "Muted", wrap=True)
        open_button = QPushButton("Open report")
        open_button.clicked.connect(self.open_report.emit)
        self.copy_button = QPushButton("Copy Markdown")
        self.copy_button.clicked.connect(self.copy_report.emit)
        export_button = QPushButton("Export .md…")
        export_button.setObjectName("Primary")
        export_button.setCursor(Qt.PointingHandCursor)
        export_button.clicked.connect(self.export_report.emit)
        actions = hbox(open_button, self.copy_button, export_button, spacing=8)
        self.summary = label("", wrap=True)
        self.summary_card = card(label("SUMMARY", "Section"), self.summary)
        self.tabs = Segmented(["Top picks", "More matches"], "Top picks")
        self.tabs.group.buttonClicked.connect(lambda _: self._show_tab())
        self.grid_holder = QWidget()
        self.grid = FlowLayout(self.grid_holder, spacing=18)
        self.notes = QVBoxLayout()
        self.notes_card = card(label("NOTES", "Section"))
        self.notes_card.layout().addLayout(self.notes)

        content = QWidget()
        content.setObjectName("Page")
        header = hbox(vbox(self.title, self.subtitle, spacing=4), vbox(actions, None), spacing=16)
        header.setStretch(0, 1)
        content.setLayout(vbox(header, self.summary_card, hbox(self.tabs, None), self.grid_holder, self.notes_card, None,
                               spacing=18, margins=(40, 32, 40, 40)))
        self.setWidget(content)
        self.output: dict = {}

    def show_output(self, output: dict):
        self.output = output
        trip = output["trip"]
        found = sum(int(m.group(1)) for entry in output["search_log"]
                    if (m := re.search(r"(\d+) listings found", entry)))
        guests = trip["adults"] + trip["children"]
        self.title.setText(f"{len(output['picks'])} picks in {trip['location']}")
        self.subtitle.setText(f"{trip['check_in']} → {trip['check_out']}  ·  {trip['nights']} nights  ·  "
                              f"{guests} guests  ·  {found} listings searched  ·  {output['model']}")
        self.summary.setText(output["summary"])
        self.summary_card.setVisible(bool(output["summary"]))
        for i, text in enumerate(("Top picks", "More matches")):
            count = len(output["picks" if i == 0 else "other_matches"])
            self.tabs.group.buttons()[i].setText(f"{text}  {count}")
        self.tabs.group.buttons()[0].setChecked(True)
        clear_layout(self.notes)
        for note in output["notes"] + [f"⚠ {w}" for w in output["warnings"]]:
            self.notes.addWidget(label(f"•  {note}", wrap=True))
        self.notes_card.setVisible(self.notes.count() > 0)
        self._show_tab()
        self.verticalScrollBar().setValue(0)

    def _show_tab(self):
        clear_layout(self.grid)
        key = "picks" if self.tabs.group.buttons()[0].isChecked() else "other_matches"
        for record in self.output.get(key, []):
            listing = ListingCard(record)
            listing.clicked.connect(self.open_record.emit)
            self.grid.addWidget(listing)
        self.grid_holder.updateGeometry()


class DetailPage(QScrollArea):
    back_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QVBoxLayout()
        content = QWidget()
        content.setObjectName("Page")
        content.setMaximumWidth(900)
        back = QPushButton("←  Back to results")
        back.setObjectName("Ghost")
        back.clicked.connect(self.back_requested.emit)
        content.setLayout(vbox(hbox(back, None), spacing=10, margins=(40, 24, 40, 40)))
        content.layout().addLayout(self.body)
        content.layout().addStretch(1)
        wrapper = QWidget()
        wrapper.setObjectName("Page")
        wrapper.setLayout(hbox(None, content, None, spacing=0))
        self.setWidget(wrapper)
        self.record: dict = {}
        self.hero: Photo | None = None
        self.thumbs: list[Photo] = []

    def show_record(self, record: dict):
        self.record = record
        clear_layout(self.body)
        self.body.setSpacing(16)

        self.hero = Photo(None, 440, radius=18)
        self.hero.setCursor(Qt.PointingHandCursor)
        self.hero.setToolTip("Open full-size photo")
        self.hero.clicked.connect(self._open_hero)
        self.body.addWidget(self.hero)
        self.thumbs = []
        if len(record["images"]) > 1:
            strip = QWidget()
            strip_layout = hbox(spacing=8)
            for i, image in enumerate(record["images"]):
                thumb = Photo(112, 76, radius=10)
                thumb.set_url(image["url"], 240)
                thumb.setCursor(Qt.PointingHandCursor)
                thumb.setToolTip(image["caption"] or f"Photo {i + 1}")
                thumb.clicked.connect(lambda i=i: self._select_photo(i))
                strip_layout.addWidget(thumb)
                self.thumbs.append(thumb)
            strip_layout.addStretch(1)
            strip.setLayout(strip_layout)
            scroller = QScrollArea()
            scroller.setWidget(strip)
            scroller.setWidgetResizable(True)
            scroller.setFixedHeight(96)
            scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.body.addWidget(scroller)
        self.caption = label("", "Muted", wrap=True)
        self.body.addWidget(self.caption)
        self._select_photo(0)

        if record.get("headline"):
            self.body.addWidget(label(f"#{record['rank']}  ·  {record['headline']}", "H1", wrap=True))
        self.body.addWidget(label(record["name"], "H2", wrap=True))
        facts = [record["property_type"], record["layout"]]
        if record["sleeps"]:
            facts.append(f"sleeps {record['sleeps']}")
        self.body.addWidget(label("  ·  ".join(f for f in facts if f), "Muted", wrap=True))

        price_row = hbox(label(f"${record['price_per_night']:,} night", "H2"),
                         label(f"${record['total_price']:,} total", "Muted"), label("   " + rating_text(record)),
                         None, spacing=10)
        airbnb = QPushButton("Open on Airbnb")
        airbnb.setObjectName("Primary")
        airbnb.setCursor(Qt.PointingHandCursor)
        airbnb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(record["url"])))
        copy = QPushButton("Copy link")
        copy.clicked.connect(lambda: (QGuiApplication.clipboard().setText(record["url"]), copy.setText("Copied ✓")))
        price_row.addWidget(copy)
        price_row.addWidget(airbnb)
        self.body.addLayout(price_row)
        if badges_for(record):
            self.body.addWidget(flow_of(badges_for(record), "Badge"))

        if record.get("why_it_fits"):
            self.body.addWidget(card(label("WHY IT FITS", "Section"), label(record["why_it_fits"], wrap=True)))
        if record.get("pros") or record.get("cons"):
            pros = card(label("PROS", "Section"),
                        *[self._bullet("✓", p, THEME["good"]) for p in record.get("pros", [])])
            cons = card(label("CONS", "Section"),
                        *[self._bullet("–", c, THEME["bad"]) for c in record.get("cons", [])])
            for column in (pros, cons):
                column.layout().addStretch(1)
            self.body.addLayout(hbox(pros, cons, spacing=16))
        if record["notable_amenities"]:
            self.body.addWidget(card(label("NOTABLE AMENITIES", "Section"),
                                     flow_of(record["notable_amenities"], "Tag")))
        self.verticalScrollBar().setValue(0)

    def _bullet(self, mark: str, text: str, color: str) -> QWidget:
        holder = QWidget()
        icon = label(mark)
        icon.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 11pt;")
        icon.setFixedWidth(18)
        icon.setAlignment(Qt.AlignTop)
        holder.setLayout(hbox(icon, label(text, wrap=True), spacing=6))
        holder.layout().setStretch(1, 1)
        return holder

    def _select_photo(self, index: int):
        images = self.record["images"]
        if not images:
            self.hero.set_url(None, 0)
            return
        self.hero.set_url(images[index]["url"], 1440)
        self.hero.index = index
        self.caption.setText(images[index]["caption"] or "")
        self.caption.setVisible(bool(images[index]["caption"]))
        for i, thumb in enumerate(self.thumbs):
            thumb.selected = i == index
            thumb.update()

    def _open_hero(self):
        images = self.record["images"]
        if images:
            QDesktopServices.openUrl(QUrl(sized_image(images[getattr(self.hero, "index", 0)]["url"], 1920)))


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------
class SearchWorker(QObject):
    log = Signal(str)
    finished = Signal(object, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, criteria, model: str, think):
        super().__init__()
        self.criteria, self.model, self.think = criteria, model, think
        self.stop_event = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            path, output = engine.run_search(self.criteria, self.model, self.think, on_log=self.log.emit,
                                             should_stop=self.stop_event.is_set)
            self.finished.emit(path, output)
        except engine.SearchCancelled:
            self.cancelled.emit()
        except Exception as ex:
            self.failed.emit(f"{type(ex).__name__}: {ex}")


class ModelLister(QObject):
    loaded = Signal(list)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        names = []
        try:
            for m in ollama.list().models:
                if "embed" in m.model:
                    continue
                capabilities = getattr(ollama.show(m.model), "capabilities", None)
                if capabilities is None or "tools" in capabilities:
                    names.append(m.model)
        except Exception:
            pass                            # Ollama not running: keep the default in the list
        self.loaded.emit(sorted(names))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stay Finder")
        self.settings = QSettings("NRSStayFinder", "StayFinder")
        try:
            saved = eval_saved(self.settings.value("last_search", ""))
        except Exception:
            saved = {}

        self.sidebar = Sidebar(saved)
        self.sidebar.search_requested.connect(self.start_search)
        self.empty = EmptyPage()
        self.running = RunningPage()
        self.running.stop_requested.connect(self.stop_search)
        self.running.back_requested.connect(lambda: self.stack.setCurrentWidget(
            self.results if self.results.output else self.empty))
        self.results = ResultsPage()
        self.results.open_record.connect(self.show_detail)
        self.results.open_report.connect(lambda: self.report_path and open_path(self.report_path))
        self.results.export_report.connect(self.export_report)
        self.results.copy_report.connect(self.copy_report)
        self.detail = DetailPage()
        self.detail.back_requested.connect(lambda: self.stack.setCurrentWidget(self.results))

        self.stack = QStackedWidget()
        for widget in (self.empty, self.running, self.results, self.detail):
            self.stack.addWidget(widget)

        root = QWidget()
        root.setObjectName("Root")
        root.setLayout(hbox(self.sidebar, self.stack, spacing=0))
        self.setCentralWidget(root)
        self.setMinimumSize(1080, 680)

        QShortcut(QKeySequence("Ctrl+Return"), self, self.start_search)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.start_search)
        QShortcut(QKeySequence.Save, self, lambda: self.report_path and self.export_report())
        self.worker: SearchWorker | None = None
        self.report_path: Path | None = None
        self.started_at = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.lister = ModelLister()
        self.lister.loaded.connect(lambda names: names and self.sidebar.set_models(names))
        self.lister.start()

    def fit_to_screen(self):
        """Call after show(): sizing before the window exists gets mangled by display scaling."""
        area = self.screen().availableGeometry()
        width, height = min(1500, int(area.width() * 0.9)), min(980, int(area.height() * 0.9))
        self.setGeometry(area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2,
                         width, height)

    def start_search(self):
        if self.worker:
            return
        try:
            criteria = self.sidebar.criteria()
        except ValueError as ex:
            self.sidebar.show_error(str(ex))
            return
        self.sidebar.show_error(None)
        model = self.sidebar.model.currentText()
        self.settings.setValue("last_search", repr({"criteria": asdict(criteria), "model": model,
                                                    "think": self.sidebar.think.isChecked()}))

        self.worker = SearchWorker(criteria, model, engine.model_think_setting(model, self.sidebar.think.isChecked()))
        self.worker.log.connect(self.running.log)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        self.running.start(criteria, model)
        self.stack.setCurrentWidget(self.running)
        self.sidebar.search_button.setEnabled(False)
        self.sidebar.search_button.setText("Searching…")
        self.started_at = time.monotonic()
        self.timer.start(1000)
        self.worker.start()

    def stop_search(self):
        if self.worker:
            self.worker.stop_event.set()
            self.running.stopping()

    def _tick(self):
        seconds = int(time.monotonic() - self.started_at)
        self.running.set_elapsed(f"{seconds // 60}:{seconds % 60:02d}")

    def _done(self):
        self.timer.stop()
        self.worker = None
        self.sidebar.search_button.setEnabled(True)
        self.sidebar.search_button.setText("Search")

    def _finished(self, path: Path, output: dict):
        self._done()
        self.report_path = path
        self.results.show_output(output)
        self.stack.setCurrentWidget(self.results)

    def _failed(self, message: str):
        self._done()
        self.running.finished_badly("Search failed", message)

    def _cancelled(self):
        self._done()
        self.running.finished_badly("Search stopped", "Nothing was saved.")

    def show_detail(self, record: dict):
        self.detail.show_record(record)
        self.stack.setCurrentWidget(self.detail)

    def export_report(self):
        if not self.report_path or not self.report_path.exists():
            QMessageBox.warning(self, "Export", "The report file is missing; run the search again.")
            return
        folder = Path(self.settings.value("export_dir", str(Path.home() / "Downloads")))
        target, _ = QFileDialog.getSaveFileName(self, "Export report", str(folder / self.report_path.name),
                                                "Markdown (*.md);;All files (*)")
        if not target:
            return
        target = Path(target)
        try:
            shutil.copyfile(self.report_path, target)
        except OSError as ex:
            QMessageBox.warning(self, "Export failed", str(ex))
            return
        self.settings.setValue("export_dir", str(target.parent))
        answer = QMessageBox.information(self, "Exported", f"Saved to\n{target}", QMessageBox.Open | QMessageBox.Ok,
                                         QMessageBox.Ok)
        if answer == QMessageBox.Open:
            subprocess.Popen(["explorer", "/select,", str(target)])

    def copy_report(self):
        if self.report_path and self.report_path.exists():
            QGuiApplication.clipboard().setText(self.report_path.read_text(encoding="utf-8"))
            self.results.copy_button.setText("Copied ✓")
            QTimer.singleShot(2000, lambda: self.results.copy_button.setText("Copy Markdown"))

    def closeEvent(self, event):
        if self.worker:
            answer = QMessageBox.question(self, "Search running", "A search is still running. Quit anyway?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.stop_event.set()
        event.accept()


def eval_saved(text: str) -> dict:
    """Last search is stored as a repr'd dict of plain values; keep only known criteria fields."""
    import ast
    data = ast.literal_eval(text) if text else {}
    known = {f.name for f in fields(engine.SearchCriteria)}
    data["criteria"] = {k: v for k, v in data.get("criteria", {}).items() if k in known}
    return data


def apply_theme(app: QApplication) -> None:
    global THEME
    THEME = DARK if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "surface2"), (QPalette.AlternateBase, "surface"),
                      (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.Button, "surface2"),
                      (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "on_accent"), (QPalette.ToolTipBase, "surface"),
                      (QPalette.ToolTipText, "text"), (QPalette.PlaceholderText, "muted")):
        palette.setColor(role, QColor(THEME[key]))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET.substitute(THEME))


def main():
    global IMAGES
    app = QApplication(sys.argv)
    app.setApplicationName("Stay Finder")
    app.setFont(QFont("Segoe UI Variable Text", 10))
    apply_theme(app)
    IMAGES = ImageLoader()
    window = MainWindow()
    window.show()
    window.fit_to_screen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
