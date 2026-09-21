def s(value):
    """Return a safe string representation of a value."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le"):
            try:
                text = data.decode(encoding).strip("\x00")
            except UnicodeDecodeError:
                continue
            if text:
                return text
        return data.decode("latin-1", errors="ignore").strip("\x00")
    return str(value)


def short_display(value, limit=300):
    """Return a single display string truncated to the requested limit."""
    text = s(value).replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def format_bytes(value):
    """Format a byte count with a readable unit suffix."""
    try:
        size = int(value or 0)
    except Exception:
        return ""
    units = ("B", "KB", "MB", "GB")
    current = float(size)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.1f} {unit}" if unit != "B" else f"{int(current)} B"
        current /= 1024
    return f"{size} B"


def format_bool(value):
    """Format a truthy or falsey value as Yes or No."""
    if value in (None, ""):
        return ""
    return "Yes" if bool(value) else "No"
