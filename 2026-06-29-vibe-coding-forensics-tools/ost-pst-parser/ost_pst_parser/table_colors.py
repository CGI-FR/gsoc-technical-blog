from PySide6.QtGui import QColor, QBrush, QLinearGradient
from hashlib import sha256
from re import findall
from .local_config import INTERNAL_EMAIL_DOMAINS

ADDRESS_EXTERNAL_BACKGROUND = QColor("#FFE08A")
ADDRESS_PERSONAL_BACKGROUND = QColor("#FCA5A5")
ATTACHMENT_BACKGROUND = QColor("#BBF7D0")
DEFAULT_DARK_TEXT = QColor("#111827")


def dark_text_brush():
    """Return a dark text brush."""
    return QBrush(DEFAULT_DARK_TEXT)


def external_mail_color(address):
    domains = set(findall('@([^ ;<>]+)',address))
    if domains == set():
        domain = address
    else:
        # drop internal domains
        domains.difference_update(INTERNAL_EMAIL_DOMAINS)
        # get a unique value for included domains
        domain = ''.join(sorted(domains))
    domain = domain.strip().lower()
    digest = sha256(bytes(domain,'utf-8')).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    saturation = 150 + digest[2] % 70
    value = 135 + digest[3] % 70
    return QColor.fromHsv(hue, saturation, value)


def external_address_brush(address):
    """Return the external address brush. It's a gradient bvetween the External background and a per-domain color."""

    gradient = QLinearGradient(0,0,0,10)
    gradient.setColorAt(0, external_mail_color(address))
    gradient.setColorAt(1, ADDRESS_EXTERNAL_BACKGROUND)

    return QBrush(gradient)


def personal_address_brush(address):
    """Return the personal address brush."""

    gradient = QLinearGradient(0,0,0,10)
    gradient.setColorAt(0, external_mail_color(address))
    gradient.setColorAt(1, ADDRESS_PERSONAL_BACKGROUND)

    return QBrush(gradient)


def attachment_brush():
    """Return the background brush used for messages with attachments."""
    return QBrush(ATTACHMENT_BACKGROUND)


def heat_color(count, scale_max):
    """Return the background and foreground colors for a scaled count."""
    ratio = count / max(scale_max, 1)
    if ratio < 0.5:
        local_ratio = ratio * 2
        red = int(99 + (255 - 99) * local_ratio)
        green = int(190 + (235 - 190) * local_ratio)
        blue = int(123 + (132 - 123) * local_ratio)
    else:
        local_ratio = (ratio - 0.5) * 2
        red = int(255 + (153 - 255) * local_ratio)
        green = int(235 + (27 - 235) * local_ratio)
        blue = int(132 + (27 - 132) * local_ratio)
    return QColor(red, green, blue), DEFAULT_DARK_TEXT


def heat_brushes(count, scale_max):
    """Return brushes for a heat-map background and foreground pair."""
    background, foreground = heat_color(count, scale_max)
    return QBrush(background), QBrush(foreground)
