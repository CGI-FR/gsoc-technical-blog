import logging
import os
import sys
import ctypes


LOGGER_NAME = "outlook_reader"
LOGGER = logging.getLogger(LOGGER_NAME)
APP_ICON_FILENAMES = ("cheese.ico", "cheese.png", "cheese.svg")
_QT_MESSAGE_FILTER_INSTALLED = False
_QT_PREVIOUS_MESSAGE_HANDLER = None


def configure_logging():
    """Configure command-line logging once."""
    if LOGGER.handlers:
        return LOGGER
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return LOGGER


def log_event(message, level="INFO"):
    """Write an operation log to stdout."""
    logger = configure_logging()
    log_level = getattr(logging, str(level).upper(), logging.INFO)
    logger.log(log_level, message)


def log_error(title, detail=""):
    """Write an error log to stdout."""
    detail = str(detail or "").strip()
    if detail:
        configure_logging().error("%s: %s", title, detail)
    else:
        configure_logging().error(title)


def pause_for_console(prompt="Press Enter to exit..."):
    """Keep console-launched errors visible for double-click users."""
    if os.environ.get("OUTLOOK_READER_BATCH_LAUNCHER"):
        return
    try:
        input(f"\n{prompt}")
    except (EOFError, OSError):
        pass


def app_icon_path():
    """Return the bundled application icon path if present."""
    root = os.path.dirname(__file__)
    for filename in APP_ICON_FILENAMES:
        path = os.path.join(root, filename)
        if os.path.exists(path):
            return path
    return ""


def normalize_native_path(path):
    """Return a native absolute path on Windows; leave other platforms untouched."""
    if not path or os.name != "nt":
        return path
    return os.path.abspath(os.path.normpath(os.path.expanduser(str(path))))


def apply_qt_app_icon(target):
    """Apply the bundled Qt icon to an application or window."""
    path = app_icon_path()
    if not path:
        return
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ost-past-parser.viewer")
        except Exception:
            pass
    try:
        from PySide6.QtGui import QIcon
    except ModuleNotFoundError:
        return
    icon = QIcon(path)
    if not icon.isNull():
        target.setWindowIcon(icon)


def suppress_qt_font_warnings():
    """Hide noisy Qt font database warnings while preserving other Qt messages."""
    global _QT_MESSAGE_FILTER_INSTALLED
    global _QT_PREVIOUS_MESSAGE_HANDLER
    if _QT_MESSAGE_FILTER_INSTALLED:
        return
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ModuleNotFoundError:
        return

    def filtered_qt_message_handler(mode, context, message):
        category = getattr(context, "category", "")
        text = str(message or "")
        if category == "qt.text.font.db" and "OpenType support missing" in text:
            return
        if _QT_PREVIOUS_MESSAGE_HANDLER is not None:
            _QT_PREVIOUS_MESSAGE_HANDLER(mode, context, message)
            return
        prefix = f"{category}: " if category and not text.startswith(f"{category}:") else ""
        sys.stderr.write(f"{prefix}{text}\n")

    _QT_PREVIOUS_MESSAGE_HANDLER = qInstallMessageHandler(filtered_qt_message_handler)
    _QT_MESSAGE_FILTER_INSTALLED = True
