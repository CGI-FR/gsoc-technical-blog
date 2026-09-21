import logging
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import re

from .local_config import MSIP_CLASSIFICATION_LABELS, MSIP_PROPERTY_IDS as CONFIGURED_MSIP_PROPERTY_IDS

try:
    import pypff
except ImportError:
    pypff = None


DEBUG_LOGGING = bool(os.environ.get("OST_VIEWER_DEBUG"))
if DEBUG_LOGGING:
    logging.basicConfig(level=logging.DEBUG)


def debug_exception(context, exc):
    """Log exception."""
    if DEBUG_LOGGING:
        logging.debug("%s: %s", context, exc, exc_info=True)


def s(v):
    """Return a safe string representation of a value."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        data = bytes(v)
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le"):
            try:
                text = data.decode(encoding).strip("\x00")
            except UnicodeDecodeError:
                continue
            if text:
                return text
        return data.decode("latin-1", errors="ignore").strip("\x00")
    return str(v)


def get_value(obj, *names):
    """Return value."""
    for name in names:
        try:
            value = getattr(obj, name)
            if callable(value):
                value = value()
        except Exception:
            continue
        if value not in (None, ""):
            return value
    return None


def iter_record_entries(item):
    """Yield record entries."""
    try:
        count = int(get_value(item, "number_of_record_sets", "get_number_of_record_sets") or 0)
    except Exception as exc:
        debug_exception("Unable to read record set count", exc)
        return

    for record_set_index in range(count):
        try:
            record_set = item.get_record_set(record_set_index)
            entry_count = int(get_value(record_set, "number_of_entries", "get_number_of_entries") or 0)
        except Exception as exc:
            debug_exception(f"Unable to read record set {record_set_index}", exc)
            continue

        for entry_index in range(entry_count):
            try:
                yield record_set.get_entry(entry_index)
            except Exception as exc:
                debug_exception(f"Unable to read record entry {record_set_index}:{entry_index}", exc)
                continue


def prop_id(entry_type):
    """Return the property id."""
    try:
        value = int(entry_type)
    except Exception:
        return None
    return value >> 16 if value > 0xFFFF else value


def entry_value(entry):
    """Return the entry value."""
    for attr in (
        "data_as_string",
        "get_data_as_string",
        "data_as_datetime",
        "get_data_as_datetime",
        "data_as_integer",
        "get_data_as_integer",
        "data",
        "get_data",
    ):
        value = get_value(entry, attr)
        if value not in (None, b"", ""):
            return value
    return None


def record_value(item, property_ids):
    """Return the first non-empty value for any requested record property."""
    wanted = set(property_ids)
    for entry in iter_record_entries(item):
        entry_type = get_value(entry, "entry_type", "get_entry_type")
        if prop_id(entry_type) in wanted or entry_type in wanted:
            value = entry_value(entry)
            if value not in (None, b"", ""):
                return value
    return None


def record_raw_value(item, property_ids):
    """Return the raw stored value for any requested record property."""
    wanted = set(property_ids)
    for entry in iter_record_entries(item):
        entry_type = get_value(entry, "entry_type", "get_entry_type")
        if prop_id(entry_type) in wanted or entry_type in wanted:
            value = get_value(entry, "data", "get_data")
            if value not in (None, b"", ""):
                return value
            value = entry_value(entry)
            if value not in (None, b"", ""):
                return value
    return None


def looks_like_html_body(value):
    """Return whether a decoded value looks like an HTML message body."""
    text = s(value).lower()
    return any(marker in text for marker in ("<html", "<body", "<!doctype html", "<table", "<div"))


def record_html_fallback(item, codepage=None):
    """Scan all record properties for an HTML-looking body payload."""
    for entry in iter_record_entries(item):
        raw_value = get_value(entry, "data", "get_data")
        value = raw_value if raw_value not in (None, b"", "") else entry_value(entry)
        if value in (None, b"", ""):
            continue
        decoded = decode_body_value(value, is_html=True, codepage=codepage)
        if looks_like_html_body(decoded):
            return value
    return None


def sub_items(item):
    """Return child items exposed by a PFF item."""
    try:
        count = int(get_value(item, "number_of_sub_items", "get_number_of_sub_items") or 0)
    except Exception as exc:
        debug_exception("Unable to read sub item count", exc)
        return []

    out = []
    for index in range(count):
        try:
            out.append(item.get_sub_item(index))
        except Exception as exc:
            debug_exception(f"Unable to read sub item {index}", exc)
            continue
    return out


def folder_name(folder, fallback="Unnamed"):
    """Return folder name."""
    return s(get_value(folder, "name", "get_name")).strip() or fallback


def folder_message_count(folder):
    """Return folder message count."""
    try:
        return int(get_value(folder, "number_of_sub_messages", "get_number_of_sub_messages") or 0)
    except Exception as exc:
        debug_exception("Unable to read folder message count", exc)
        return 0


def folder_subfolder_count(folder):
    """Return folder subfolder count."""
    try:
        return int(get_value(folder, "number_of_sub_folders", "get_number_of_sub_folders") or 0)
    except Exception as exc:
        debug_exception("Unable to read subfolder count", exc)
        return 0


def folder_subfolder_at(folder, index):
    """Return folder subfolder at."""
    try:
        return folder.get_sub_folder(index)
    except Exception as exc:
        debug_exception(f"Unable to read subfolder {index}", exc)
        return None


def create_pff_reader():
    """Create a libpff reader or explain how to install the correct binding."""
    if pypff is None:
        raise RuntimeError(
            "The libpff Python bindings are not installed.\n\n"
            "Install the package that provides the Outlook PST/OST bindings, "
            "then run this app with that same Python environment.\n\n"
            "Recommended macOS steps:\n"
            "  python3 -m pip uninstall pypff\n"
            "  python3 -m pip install PySide6 libpff-python\n\n"
            "If pip cannot build or find a wheel for your Mac/Python version, "
            "install libpff from https://github.com/libyal/libpff and enable "
            "its Python bindings."
        )

    reader_cls = getattr(pypff, "file", None)
    if reader_cls is None:
        module_path = getattr(pypff, "__file__", "unknown location")
        raise RuntimeError(
            "The imported 'pypff' module is not the Outlook PST/OST parser.\n\n"
            f"Imported module: {module_path}\n\n"
            "There is an unrelated PyPI package named 'pypff' for PanoSETI files. "
            "Remove it and install the libpff bindings instead:\n\n"
            "  python3 -m pip uninstall pypff\n"
            "  python3 -m pip install libpff-python\n\n"
            "The correct module exposes pypff.file()."
        )

    return reader_cls()

# =========================================================
# RECIPIENT FIX
# =========================================================
CONTACT_DISPLAY_NAME_PROPERTIES = {0x3001, 0x808A, 0x84BC, 0x84BF}
CONTACT_EMAIL_PROPERTIES = {0x8038, 0x808D, 0x39FE, 0x3003, 0x5D0B}
SENDER_DISPLAY_NAME_PROPERTIES = {0x0C1A, 0x0042, 0x4038, 0x4039, 0x3001}
SENDER_EMAIL_PROPERTIES = {0x5D01, 0x5D02, 0x0C1F, 0x0065, 0x39FE, 0x3003}
RECIPIENT_DISPLAY_NAME_PROPERTIES = {0x5D0A, 0x3001, 0x0E04, 0x0E03, 0x0E02}
RECIPIENT_EMAIL_PROPERTIES = {0x39FE, 0x3003, 0x5D0B, 0x5D0A, 0x5D07, 0x5D08}
RECIPIENT_CACHE_FOLDER_NAME = "Recipient Cache"
RECIPIENT_TO = 1
RECIPIENT_CC = 2
RECIPIENT_BCC = 3


def normalized_contact_key(value):
    """Return a normalized key for contact-cache lookups."""
    return re.sub(r"\s+", " ", s(value)).strip().lower()


def looks_like_email(value):
    """Return whether like email."""
    text = s(value).strip()
    return bool(re.match(r"^[^@\s;<>]+@[^@\s;<>]+\.[^@\s;<>]+$", text))


def format_contact(name, email):
    """Format contact."""
    name = s(name).strip()
    email = s(email).strip()
    if name and email and normalized_contact_key(name) != normalized_contact_key(email):
        return f"{name} <{email}>"
    return email or name


def first_value(item, property_ids):
    """Return the first value."""
    for property_id in property_ids:
        value = record_value(item, {property_id})
        if s(value).strip():
            return value
    return ""


def first_email_value(item, property_ids):
    """Return the first email value."""
    for property_id in property_ids:
        value = s(record_value(item, {property_id})).strip()
        if looks_like_email(value):
            return value
    return ""


def contact_email_for(value, contact_cache=None):
    """Return contact email for."""
    if not contact_cache:
        return ""
    return contact_cache.get(normalized_contact_key(value), "")


def enrich_contact_value(value, contact_cache=None):
    """Enrich contact value."""
    text = s(value).strip()
    if not text or looks_like_email(text) or "<" in text:
        return text
    return format_contact(text, contact_email_for(text, contact_cache))


def enrich_contact_list(value, contact_cache=None):
    """Enrich contact list."""
    text = s(value).strip()
    if not text:
        return ""
    parts = [part.strip() for part in re.split(r";\s*", text) if part.strip()]
    if len(parts) <= 1:
        return enrich_contact_value(text, contact_cache)
    return "; ".join(enrich_contact_value(part, contact_cache) for part in parts)


def normalized_recipient_type(value):
    """Return a normalized recipient type."""
    try:
        return int(value)
    except Exception:
        text = s(value).strip().lower()
        if text in ("to", "mapi_to"):
            return RECIPIENT_TO
        if text in ("cc", "mapi_cc"):
            return RECIPIENT_CC
        if text in ("bcc", "mapi_bcc"):
            return RECIPIENT_BCC
    return None


def add_contact_cache_entry(contact_cache, display_name, email):
    """Add contact cache entry."""
    display_name = s(display_name).strip()
    email = s(email).strip()
    if not display_name or not email:
        return
    contact_cache.setdefault(normalized_contact_key(display_name), email)


def contact_cache_entry(item):
    """Return contact cache entry."""
    display_name = first_value(item, CONTACT_DISPLAY_NAME_PROPERTIES)
    email = first_email_value(item, CONTACT_EMAIL_PROPERTIES)
    return s(display_name).strip(), s(email).strip()


def message_class(item):
    """Return message class."""
    return s(get_value(item, "message_class", "get_message_class") or record_value(item, {0x001A})).strip()


def contact_display(item):
    """Return contact display."""
    if message_class(item).lower() != "ipm.contact":
        return ""
    display_name, email = contact_cache_entry(item)
    return format_contact(display_name, email)


def build_recipient_cache(root_folder, deadline=None, return_completed=False):
    """Build recipient cache."""
    contact_cache = {}

    def visit(folder):
        """Visit folders recursively while building the recipient cache."""
        if deadline is not None and time.monotonic() >= deadline:
            return False

        if folder_name(folder) == RECIPIENT_CACHE_FOLDER_NAME:
            for index in range(folder_message_count(folder)):
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                try:
                    display_name, email = contact_cache_entry(folder.get_sub_message(index))
                except Exception as exc:
                    debug_exception(f"Unable to read recipient cache entry {index}", exc)
                    continue
                add_contact_cache_entry(contact_cache, display_name, email)

        for index in range(folder_subfolder_count(folder)):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            subfolder = folder_subfolder_at(folder, index)
            if subfolder is not None:
                if not visit(subfolder):
                    return False
        return True

    completed = visit(root_folder)
    if return_completed:
        return contact_cache, completed
    return contact_cache


def resolve_sender(msg, contact_cache=None):
    """Resolve sender."""
    email = (
        get_value(msg, "sender_email_address", "get_sender_email_address")
        or first_email_value(msg, SENDER_EMAIL_PROPERTIES)
    )
    name = (
        get_value(msg, "sender_name", "get_sender_name")
        or get_value(msg, "sent_representing_name", "get_sent_representing_name")
        or first_value(msg, SENDER_DISPLAY_NAME_PROPERTIES)
    )
    if email or name:
        return format_contact(name, email) if email and name else enrich_contact_value(email or name, contact_cache)

    return ""


def recipient_display_value(recipient, contact_cache=None):
    """Return a recipient display string with contact-cache enrichment."""
    email = (
        get_value(recipient, "email_address", "get_email_address", "smtp_address", "get_smtp_address")
        or first_email_value(recipient, RECIPIENT_EMAIL_PROPERTIES)
    )
    name = (
        get_value(recipient, "name", "get_name", "display_name", "get_display_name")
        or first_value(recipient, RECIPIENT_DISPLAY_NAME_PROPERTIES)
    )
    if email and name:
        return format_contact(name, email)
    return enrich_contact_value(email or name, contact_cache)


def recipient_type(recipient):
    """Return the normalized recipient type for a recipient row."""
    return normalized_recipient_type(get_value(
        recipient,
        "recipient_type",
        "get_recipient_type",
        "type",
        "get_type",
    ))


def resolve_recipients(msg, wanted_type, display_property_id, display_attr, contact_cache=None):
    """Resolve recipients of one type from typed rows, display fields, or subitems."""
    out = []
    saw_typed_recipients = False
    # Prefer explicit recipient rows because they preserve To/Cc/Bcc types.
    try:
        if hasattr(msg, "number_of_recipients"):
            for i in range(msg.number_of_recipients):
                r = msg.get_recipient(i)
                r_type = recipient_type(r)
                saw_typed_recipients = saw_typed_recipients or r_type is not None
                if r_type is not None and r_type != wanted_type:
                    continue
                if r_type is None and wanted_type != RECIPIENT_TO:
                    continue
                val = recipient_display_value(r, contact_cache)
                if val:
                    out.append(val)
    except Exception as exc:
        debug_exception("Unable to read message recipients", exc)

    if out or (saw_typed_recipients and wanted_type != RECIPIENT_TO):
        return "; ".join(out)

    # Fall back to Outlook's display fields when typed recipient rows are absent.
    display_value = get_value(msg, display_attr, f"get_{display_attr}") or record_value(msg, {display_property_id})
    if display_value:
        return enrich_contact_list(display_value, contact_cache)

    # Some libpff builds expose recipient records as message subitems instead.
    seen = set()
    for item in sub_items(msg):
        item_type = normalized_recipient_type(record_value(item, {0x0C15}))
        if item_type != wanted_type:
            continue

        value = (
            format_contact(
                first_value(item, RECIPIENT_DISPLAY_NAME_PROPERTIES),
                first_email_value(item, RECIPIENT_EMAIL_PROPERTIES),
            )
            or enrich_contact_value(first_value(item, RECIPIENT_DISPLAY_NAME_PROPERTIES), contact_cache)
        )
        if value and value not in seen:
            out.append(value)
            seen.add(value)

    return "; ".join(out)


def resolve_to(msg, contact_cache=None):
    """Resolve to."""
    return resolve_recipients(msg, RECIPIENT_TO, 0x0E04, "display_to", contact_cache)


def resolve_cc(msg, contact_cache=None):
    """Resolve cc."""
    return resolve_recipients(msg, RECIPIENT_CC, 0x0E03, "display_cc", contact_cache)


def resolve_bcc(msg, contact_cache=None):
    """Resolve bcc."""
    return resolve_recipients(msg, RECIPIENT_BCC, 0x0E02, "display_bcc", contact_cache)


def codepage_encoding(value):
    """Return a Python codec name for a MAPI codepage value."""
    text = s(value).strip()
    if not text:
        return ""
    if text == "65001":
        return "utf-8"
    try:
        return f"cp{int(text)}"
    except Exception:
        return text


RTF_COMPRESSED_PRELOAD = (
    b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}"
    b"{\\f0\\fnil \\froman \\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New RomanCourier"
    b"{\\colortbl\\red0\\green0\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
)


def decompress_rtf_lzfu(value):
    """Return decompressed RTF bytes for an Outlook LZFu payload when possible."""
    if not isinstance(value, (bytes, bytearray)):
        return None
    data = bytes(value)
    if len(data) < 16:
        return None
    try:
        compressed_size, uncompressed_size, magic, _crc = struct.unpack("<LLLL", data[:16])
    except Exception:
        return None

    # 0x414C454D = "MELA" marks uncompressed RTF after the header.
    if magic == 0x414C454D:
        return data[16:16 + uncompressed_size] if uncompressed_size else data[16:]
    # 0x75465A4C = "LZFu" marks compressed RTF.
    if magic != 0x75465A4C:
        return None

    payload_end = 16 + compressed_size - 12 if compressed_size >= 12 else len(data)
    payload = data[16:min(payload_end, len(data))]
    dictionary = bytearray(4096)
    preload = RTF_COMPRESSED_PRELOAD[:4096]
    dictionary[:len(preload)] = preload
    write_pos = len(preload) % 4096
    output = bytearray()
    read_index = 0

    try:
        nonlocal_write_pos = [write_pos]

        def emit_byte(byte):
            output.append(byte)
            dictionary[nonlocal_write_pos[0]] = byte
            nonlocal_write_pos[0] = (nonlocal_write_pos[0] + 1) % 4096

        while read_index < len(payload) and (not uncompressed_size or len(output) < uncompressed_size):
            flags = payload[read_index]
            read_index += 1
            for bit in range(8):
                if read_index >= len(payload) or (uncompressed_size and len(output) >= uncompressed_size):
                    break
                if flags & (1 << bit):
                    if read_index + 1 >= len(payload):
                        break
                    first = payload[read_index]
                    second = payload[read_index + 1]
                    read_index += 2
                    offset = (first << 4) | (second >> 4)
                    length = (second & 0x0F) + 2
                    for _ in range(length):
                        byte = dictionary[offset % 4096]
                        emit_byte(byte)
                        offset += 1
                else:
                    byte = payload[read_index]
                    read_index += 1
                    emit_byte(byte)
        return bytes(output)
    except Exception as exc:
        debug_exception("Unable to decompress RTF body", exc)
        return None


def decode_body_value(value, is_html=False, codepage=None):
    """Decode a body payload using BOMs, declared charsets, and common fallbacks."""
    if value is None:
        return ""
    if not isinstance(value, (bytes, bytearray)):
        return s(value).strip()

    data = bytes(value)
    if not data:
        return ""

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace").strip("\x00").strip()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace").strip("\x00").strip()

    even_nulls = data[0::2].count(0)
    odd_nulls = data[1::2].count(0)
    half_length = max(len(data) // 2, 1)
    if odd_nulls / half_length > 0.25 and even_nulls / half_length < 0.05:
        return data.decode("utf-16-le", errors="replace").strip("\x00").strip()
    if even_nulls / half_length > 0.25 and odd_nulls / half_length < 0.05:
        return data.decode("utf-16-be", errors="replace").strip("\x00").strip()

    # HTML bodies can declare a charset inside the payload; try that before
    # falling back to encodings commonly seen in Outlook archives.
    candidates = []
    if is_html:
        head = data[:4096].decode("ascii", errors="ignore")
        for pattern in (
            r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)",
            r"encoding\s*=\s*['\"]([A-Za-z0-9._-]+)",
        ):
            candidates.extend(re.findall(pattern, head, flags=re.IGNORECASE))

    preferred_encoding = codepage_encoding(codepage)
    if preferred_encoding:
        candidates.append(preferred_encoding)
    candidates.extend(("utf-8", "windows-1252", "latin-1"))
    seen = set()
    for encoding in candidates:
        encoding = encoding.strip().lower()
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        return text.strip("\x00").strip()

    return data.decode("latin-1", errors="replace").strip("\x00").strip()


def resolve_body(msg):
    """Resolve body."""
    body_codepage = record_value(msg, {0x3FDE, 0x3FFD})
    html = (
        get_value(msg, "html_body", "get_html_body")
        or record_raw_value(msg, {0x1013})
    )
    if not html:
        html = record_html_fallback(msg, body_codepage)
    text = (
        get_value(msg, "plain_text_body", "get_plain_text_body")
        or record_raw_value(msg, {0x1000})
        or record_raw_value(msg, {0x3FD9})
        or record_raw_value(msg, {0x6619})
    )
    rtf = (
        get_value(msg, "rtf_body", "get_rtf_body")
        or record_raw_value(msg, {0x1009})
    )
    decompressed_rtf = decompress_rtf_lzfu(rtf)
    if decompressed_rtf:
        rtf = decompressed_rtf

    return (
        decode_body_value(html, is_html=True, codepage=body_codepage),
        decode_body_value(text, codepage=body_codepage),
        decode_body_value(rtf, codepage=body_codepage),
    )


PROPERTY_NAMES = {
    0x0001: "Acknowledgement Mode",
    0x0002: "Alternate Recipient Allowed",
    0x0004: "Auto Forward Comment",
    0x0005: "Auto Forwarded",
    0x0007: "Content Confidentiality Algorithm ID",
    0x0008: "Content Correlator",
    0x0009: "Content Identifier",
    0x000A: "Content Length",
    0x000B: "Content Return Requested",
    0x000C: "Conversation Key",
    0x000D: "Conversion Eits",
    0x000E: "Conversion With Loss Prohibited",
    0x000F: "Converted Eits",
    0x0010: "Deferred Delivery Time",
    0x0011: "Deliver Time",
    0x0012: "Discard Reason",
    0x0013: "Disclosure Of Recipients",
    0x0014: "Distribution List Expansion History",
    0x0015: "Distribution List Expansion Prohibited",
    0x0017: "Importance",
    0x001A: "Message Class",
    0x0023: "Originator Delivery Report Requested",
    0x0025: "Parent Key",
    0x0026: "Priority",
    0x0029: "Read Receipt Requested",
    0x002A: "Receipt Time",
    0x002B: "Recipient Reassignment Prohibited",
    0x002E: "Original Sensitivity",
    0x0030: "Reply Time",
    0x0031: "Report Tag",
    0x0032: "Report Time",
    0x0036: "Sensitivity",
    0x0037: "Subject",
    0x0039: "Client Submit Time",
    0x003B: "Sent Representing Search Key",
    0x003D: "Subject Prefix",
    0x0042: "Sent Representing Name",
    0x0043: "Received By Entry ID",
    0x0044: "Received By Name",
    0x0045: "Sent Representing Entry ID",
    0x0046: "Read Receipt Entry ID",
    0x0047: "Report Entry ID",
    0x0048: "Read Receipt Search Key",
    0x0049: "Report Search Key",
    0x004B: "Original Message Class",
    0x004C: "Original Author Entry ID",
    0x004D: "Original Author Name",
    0x004E: "Original Submit Time",
    0x004F: "Reply Recipient Entries",
    0x0050: "Reply Recipient Names",
    0x0051: "Received By Search Key",
    0x0052: "Received Representing Entry ID",
    0x0053: "Received Representing Name",
    0x0054: "Received Representing Search Key",
    0x0055: "Report Text",
    0x0057: "Message To Me",
    0x0058: "Message CC Me",
    0x0059: "Message Recipient Me",
    0x005A: "Original Sender Name",
    0x005B: "Original Sender Entry ID",
    0x005C: "Original Sender Search Key",
    0x005D: "Original Sent Representing Name",
    0x005E: "Original Sent Representing Entry ID",
    0x005F: "Original Sent Representing Search Key",
    0x0060: "Start Date",
    0x0061: "End Date",
    0x0062: "Owner Appointment ID",
    0x0063: "Response Requested",
    0x0064: "Sent Representing Address Type",
    0x0065: "Sent Representing Email Address",
    0x0070: "Conversation Topic",
    0x0071: "Conversation Index",
    0x0072: "Original Display Bcc",
    0x0073: "Original Display Cc",
    0x0074: "Original Display To",
    0x0075: "Received By Address Type",
    0x0076: "Received By Email Address",
    0x0077: "Received Representing Address Type",
    0x0078: "Received Representing Email Address",
    0x007D: "Transport Message Headers",
    0x007F: "TNEF Correlation Key",
    0x0C15: "Recipient Type",
    0x0C19: "Sender Entry ID",
    0x0C1A: "Sender Name",
    0x0C1D: "Sender Search Key",
    0x0C1F: "Sender Email Address",
    0x0C1E: "Sender Address Type",
    0x0E01: "Delete After Submit",
    0x0E02: "Display Bcc",
    0x0E03: "Display Cc",
    0x0E04: "Display To",
    0x0E05: "Parent Display",
    0x0E06: "Message Delivery Time",
    0x0E07: "Message Flags",
    0x0E08: "Message Size",
    0x0E09: "Parent Entry ID",
    0x0E0A: "Sent Mail Entry ID",
    0x0E0F: "Responsibility",
    0x0E12: "Message Recipients",
    0x0E13: "Message Attachments",
    0x0E17: "Message Status",
    0x0E1B: "Has Attachments",
    0x0E1D: "Normalized Subject",
    0x0E1F: "RTF In Sync",
    0x0E20: "Attachment Size",
    0x0E21: "Attach Number",
    0x0E23: "Internet Article Number",
    0x0E27: "Security",
    0x0E28: "Primary Send Account",
    0x0E29: "Next Send Account",
    0x0E2B: "To Do Item Flags",
    0x0E69: "Read",
    0x0E79: "Trust Sender",
    0x0FF4: "Access",
    0x0FF5: "Row Type",
    0x0FF6: "Instance Key",
    0x0FF7: "Access Level",
    0x0FF8: "Mapping Signature",
    0x0FF9: "Record Key",
    0x0FFA: "Store Record Key",
    0x0FFB: "Store Entry ID",
    0x0FFE: "Object Type",
    0x0FFF: "Entry ID",
    0x1000: "Plain Text Body",
    0x1006: "RTF Sync Body CRC",
    0x1007: "RTF Sync Body Count",
    0x1008: "RTF Sync Body Tag",
    0x1009: "RTF Body",
    0x1010: "RTF Sync Prefix Count",
    0x1011: "RTF Sync Trailing Count",
    0x1012: "Originally Intended Recipient Name",
    0x1013: "HTML Body",
    0x1014: "Body Content Location",
    0x1015: "Body Content ID",
    0x1035: "Internet Message ID",
    0x1039: "Internet References",
    0x1042: "In Reply To ID",
    0x1046: "Original Internet Message ID",
    0x1080: "Icon Index",
    0x1081: "Last Verb Executed",
    0x1082: "Last Verb Execution Time",
    0x1090: "Flag Status",
    0x1091: "Flag Complete Time",
    0x1095: "Followup Icon",
    0x1096: "Block Status",
    0x10C3: "ICalendar Start Time",
    0x10C4: "ICalendar End Time",
    0x10C5: "Cdo Recurrence ID",
    0x10CA: "ICalendar Reminder Next Time",
    0x10F3: "URL Component Name",
    0x10F4: "Attribute Hidden",
    0x10F5: "Attribute System",
    0x10F6: "Attribute Read Only",
    0x3000: "Row ID",
    0x3001: "Display Name",
    0x3002: "Address Type",
    0x3003: "Email Address",
    0x3004: "Comment",
    0x3005: "Depth",
    0x3007: "Creation Time",
    0x3008: "Last Modification Time",
    0x300B: "Search Key",
    0x300F: "Default Store",
    0x3010: "Store Support Mask",
    0x3013: "Store State",
    0x3016: "Container Flags",
    0x3018: "Folder Type",
    0x3019: "Content Count",
    0x301A: "Content Unread Count",
    0x301B: "Create Templates",
    0x301C: "Details Table",
    0x301D: "Search",
    0x301E: "Selectable",
    0x301F: "Subfolders",
    0x3020: "Status",
    0x3021: "Anr",
    0x35DF: "Valid Folder Mask",
    0x35E0: "IPM Subtree Entry ID",
    0x35E2: "IPM Outbox Entry ID",
    0x35E3: "IPM Wastebasket Entry ID",
    0x35E4: "IPM Sent Mail Entry ID",
    0x35E5: "Views Entry ID",
    0x35E6: "Common Views Entry ID",
    0x35E7: "Finder Entry ID",
    0x3600: "Container Contents",
    0x3601: "Container Flags",
    0x3602: "Folder Associated Contents",
    0x3603: "Def Create DL",
    0x3604: "Def Create Mailuser",
    0x3609: "Container Class",
    0x360A: "Container Modify Version",
    0x360C: "AB Provider ID",
    0x360D: "Default View Entry ID",
    0x3613: "Hierarchy Change Number",
    0x3617: "Associated Content Count",
    0x361A: "Content Unread Count",
    0x36D8: "Last Used Folder Time",
    0x3701: "Attachment Data Binary",
    0x3703: "Attachment Extension",
    0x3704: "Attachment Filename",
    0x3705: "Attachment Method",
    0x3707: "Attachment Long Filename",
    0x3708: "Attachment Pathname",
    0x3709: "Attachment Rendering Position",
    0x370A: "Attachment Tag",
    0x370B: "Rendering Position",
    0x370C: "Attachment Transport Name",
    0x370D: "Attachment Long Pathname",
    0x370E: "Attachment MIME Type",
    0x370F: "Attachment Additional Information",
    0x3710: "Attachment MIME Sequence",
    0x3712: "Attachment Content ID",
    0x3713: "Attachment Content Location",
    0x3714: "Attachment Flags",
    0x3719: "Attachment Payload Provider GUID String",
    0x371A: "Attachment Payload Class",
    0x371B: "Text Attachment Charset",
    0x3900: "Display Type",
    0x3902: "Template ID",
    0x39FE: "SMTP Address",
    0x39FF: "Address Book Display Name Printable",
    0x3A00: "Account",
    0x3A02: "Callback Telephone Number",
    0x3A05: "Generation",
    0x3A06: "Given Name",
    0x3A08: "Business Telephone Number",
    0x3A09: "Home Telephone Number",
    0x3A0A: "Initials",
    0x3A0B: "Keyword",
    0x3A0C: "Language",
    0x3A0D: "Location",
    0x3A11: "Surname",
    0x3A15: "Postal Address",
    0x3A16: "Company Name",
    0x3A17: "Title",
    0x3A18: "Department Name",
    0x3A19: "Office Location",
    0x3A1A: "Primary Telephone Number",
    0x3A1B: "Business2 Telephone Number",
    0x3A1C: "Mobile Telephone Number",
    0x3A1D: "Radio Telephone Number",
    0x3A1E: "Car Telephone Number",
    0x3A1F: "Other Telephone Number",
    0x3A20: "Transmittable Display Name",
    0x3A21: "Pager Telephone Number",
    0x3A22: "User Certificate",
    0x3A23: "Primary Fax Number",
    0x3A24: "Business Fax Number",
    0x3A25: "Home Fax Number",
    0x3A26: "Country",
    0x3A27: "Locality",
    0x3A28: "State Or Province",
    0x3A29: "Street Address",
    0x3A2A: "Postal Code",
    0x3A2B: "Post Office Box",
    0x3A2C: "Telex Number",
    0x3A2D: "ISDN Number",
    0x3A2E: "Assistant Telephone Number",
    0x3A2F: "Home2 Telephone Number",
    0x3A30: "Assistant",
    0x3A40: "Send Rich Info",
    0x3A41: "Wedding Anniversary",
    0x3A42: "Birthday",
    0x3A43: "Hobbies",
    0x3A44: "Middle Name",
    0x3A45: "Display Name Prefix",
    0x3A46: "Profession",
    0x3A48: "Spouse Name",
    0x3A4B: "TTY TDD Phone Number",
    0x3A4C: "FTP Site",
    0x3A4E: "Manager Name",
    0x3A4F: "Nickname",
    0x3A51: "Business Home Page",
    0x3A56: "Contact Email Addresses",
    0x3A57: "Company Main Telephone Number",
    0x3A58: "Childrens Names",
    0x3A70: "User X509 Certificate",
    0x3FD9: "Plain Text Body Fallback",
    0x3FDE: "Internet Codepage",
    0x3FF8: "Creator Entry ID",
    0x3FF9: "Creator Name",
    0x3FFA: "Last Modifier Entry ID",
    0x3FFB: "Last Modifier Name",
    0x3FFD: "Message Codepage",
    0x3FFE: "Sent Representing Flags",
    0x3FFF: "Message Locale ID",
    0x4038: "Sender Display Name",
    0x4039: "Sent Representing Display Name",
    0x5D01: "Sender SMTP Address",
    0x5D02: "Sent Representing SMTP Address",
    0x5D05: "Read Receipt SMTP Address",
    0x5D07: "Received By SMTP Address",
    0x5D08: "Received Representing SMTP Address",
    0x5D09: "Recipient Order",
    0x5D0A: "Recipient Display Name",
    0x5D0B: "Recipient Entry ID",
    0x65E0: "Source Key",
    0x65E2: "Parent Source Key",
    0x6619: "Plain Text Body Fallback",
    0x66B5: "Common Start",
    0x66B6: "Common End",
    0x6743: "Conversation ID",
    0x6744: "Conversation Index Tracking",
    0x6745: "Archive Tag",
    0x6746: "Policy Tag",
    0x6748: "Retention Period",
    0x6749: "Start Date Etc",
    0x674A: "Retention Date",
    0x67F2: "Offline Address Book Name",
    0x7C06: "Roaming Datatypes",
    0x7C07: "Roaming Dictionary",
    0x7C08: "Roaming XML Stream",
    0x7D01: "Processed",
    0x8005: "Side Effects",
    0x8011: "Address Book Provider Array Type",
    0x8014: "Address Book Provider Email List",
    0x8038: "Recipient Cache Email Address",
    0x808A: "Recipient Cache Display Name",
    0x808D: "Recipient Cache Email Address",
    0x8156: "MSIP Label Properties",
    0x83D4: "MSIP Label Properties",
    0x83E3: "MSIP Label Properties",
    0x84BC: "Recipient Cache Display Name",
    0x84BF: "Recipient Cache Display Name",
}


CLASSIFICATION_LABELS = {
    s(uuid).lower(): tuple(label_info)
    for uuid, label_info in MSIP_CLASSIFICATION_LABELS.items()
}


def short_display(value, limit=1200):
    """Return a shortened display."""
    text = s(value).strip()
    if len(text) > limit:
        return text[:limit] + f"\n... ({len(text) - limit} more characters)"
    return text


def value_bytes(value):
    """Return value bytes."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return s(value).encode("utf-8", errors="replace")


def property_value_size(value):
    """Return property value size."""
    return len(value_bytes(value))


def is_plain_ascii(value):
    """Return whether plain ascii."""
    data = value_bytes(value)
    if not data:
        return True
    return all(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in data)


def hexdump(value, width=16, limit=4096):
    """Format bytes as a compact hexdump string."""
    data = value_bytes(value)
    limited = data[:limit]
    lines = []
    for offset in range(0, len(limited), width):
        chunk = limited[offset:offset + width]
        hex_part = " ".join(f"{byte:02X}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        lines.append(f"{offset:08X}  {hex_part:<{width * 3}} {ascii_part}")
    if len(data) > limit:
        lines.append(f"... ({len(data) - limit} more bytes)")
    return "\n".join(lines)


def serializable_record_entry(entry):
    """Return a serializable record entry."""
    return {
        "property_id": entry["property_id"],
        "name": entry["name"],
        "value": entry["value"],
        "size": entry["size"],
        "hexdump": entry["hexdump"],
    }


def record_entries(item, max_value=4000):
    """Return record entries."""
    entries = []
    for entry in iter_record_entries(item):
        entry_type = get_value(entry, "entry_type", "get_entry_type")
        pid = prop_id(entry_type)
        value = entry_value(entry)
        entries.append({
            "property_id": f"0x{pid:04X}" if pid is not None else s(entry_type),
            "name": PROPERTY_NAMES.get(pid, ""),
            "value": short_display(value, max_value),
            "size": property_value_size(value),
            "hexdump": "" if is_plain_ascii(value) else hexdump(value, limit=max_value),
            "data": value_bytes(value),
        })
    return entries


MSIP_PROPERTY_IDS = set(CONFIGURED_MSIP_PROPERTY_IDS)


def msip_text_matches(text):
    """Return whether text looks like Microsoft sensitivity label metadata."""
    if "msip_label_" in text:
        return True
    return any(uuid in text for uuid in CLASSIFICATION_LABELS)


def classification_from_label_properties(label_properties):
    """Return classification label and color from discovered label properties."""
    label_properties = s(label_properties).lower()
    if not label_properties:
        return "", ""

    labels = []
    color = ""
    seen = set()
    for uuid, (name, _enabled, label_color) in CLASSIFICATION_LABELS.items():
        if uuid in label_properties and name not in seen:
            labels.append(name)
            seen.add(name)
        if uuid in label_properties and label_color and not color:
            color = label_color
    return ", ".join(labels), color


def discovered_msip_label_properties(msg):
    """Return discovered msip label properties."""
    values = []
    for entry in iter_record_entries(msg):
        entry_type = get_value(entry, "entry_type", "get_entry_type")
        pid = prop_id(entry_type)
        value = entry_value(entry)
        text = s(value).lower()
        if text and msip_text_matches(text):
            if pid is not None:
                MSIP_PROPERTY_IDS.add(pid)
            values.append(text)
    return ";".join(values)


def email_classification_info(msg):
    """Build email classification info."""
    label_properties = record_value(msg, MSIP_PROPERTY_IDS)
    classification, color = classification_from_label_properties(label_properties)
    if classification:
        return classification, color

    return classification_from_label_properties(discovered_msip_label_properties(msg))


def email_classification(msg):
    """Build email classification."""
    classification, _color = email_classification_info(msg)
    return classification


def transport_headers(msg):
    """Return transport headers."""
    return s(
        get_value(
            msg,
            "transport_headers",
            "get_transport_headers",
            "internet_headers",
            "get_internet_headers",
        )
        or record_value(msg, {0x007D})
    ).strip()


def parse_header_lines(raw_headers):
    """Parse header lines."""
    rows = []
    current_name = None
    current_value = []

    for line in s(raw_headers).replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line[:1] in (" ", "\t") and current_name:
            current_value.append(line.strip())
            continue
        if current_name:
            rows.append((current_name, " ".join(current_value)))
        if ":" in line:
            current_name, value = line.split(":", 1)
            current_name = current_name.strip()
            current_value = [value.strip()]
        else:
            current_name = None
            current_value = []

    if current_name:
        rows.append((current_name, " ".join(current_value)))
    return rows


def attachment_count(msg):
    """Return attachment count."""
    try:
        return int(get_value(msg, "number_of_attachments", "get_number_of_attachments") or 0)
    except Exception:
        return 0


def truthy_mapi_value(value):
    """Return whether a MAPI-ish property value should be treated as true."""
    if value in (None, "", b""):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, (bytes, bytearray)):
        return any(byte != 0 for byte in bytes(value))
    text = s(value).strip().lower()
    return text not in ("", "0", "false", "no", "none")


def attachment_indicator_details(msg):
    """Return attachment indicators not necessarily exposed as attachment rows."""
    details = []
    api_flag = get_value(msg, "has_attachments", "get_has_attachments")
    if truthy_mapi_value(api_flag):
        details.append("libpff reports has_attachments")

    has_attachments = record_value(msg, {0x0E1B})
    if truthy_mapi_value(has_attachments):
        details.append("MAPI Has Attachments property is set")

    message_attachments = record_raw_value(msg, {0x0E13})
    if truthy_mapi_value(message_attachments):
        details.append("MAPI Message Attachments property is present")

    return details


def has_attachment_indicators(msg):
    """Return whether a message has attachment markers outside exposed rows."""
    return bool(attachment_indicator_details(msg))


def message_size(msg):
    """Return message size."""
    value = (
        get_value(msg, "message_size", "get_message_size", "size", "get_size")
        or record_value(msg, {0x0E08})
    )
    try:
        return int(value)
    except Exception:
        return None


def message_date(msg):
    """Return message date."""
    return get_value(
        msg,
        "delivery_time",
        "get_delivery_time",
        "client_submit_time",
        "get_client_submit_time",
    )


def normalized_message_timestamp(value):
    """Return a normalized message timestamp."""
    if value in (None, ""):
        return None

    if hasattr(value, "timestamp"):
        try:
            return value.timestamp()
        except Exception:
            pass

    text = s(value).strip()
    if not text:
        return None

    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except Exception:
        pass

    try:
        return parsedate_to_datetime(text).timestamp()
    except Exception:
        return None


def message_date_sort_key(msg):
    """Return message date sort key."""
    value = message_date(msg)
    timestamp = normalized_message_timestamp(value)
    if timestamp is not None:
        return "date", timestamp

    text = s(value).strip()
    if text:
        return "text", text.lower()

    return None, None


def format_bytes(value):
    """Format bytes."""
    if value is None:
        return ""
    try:
        size = float(value)
    except Exception:
        return s(value)

    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def attachment_at(msg, index):
    """Return attachment at."""
    for method_name in ("get_attachment", "get_sub_attachment"):
        method = getattr(msg, method_name, None)
        if not method:
            continue
        try:
            return method(index)
        except Exception:
            continue
    return None


def attachment_bytes(att):
    """Return attachment bytes using direct buffers or streamed reads."""
    # Newer bindings often expose the full payload directly.
    for attr in ("data", "get_data", "buffer", "get_buffer"):
        value = get_value(att, attr)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)

    # Older bindings may only support stream-style reads from the attachment.
    size = get_value(att, "size", "get_size") or record_value(att, {0x0E20})
    try:
        size = int(size)
    except Exception:
        size = 0

    seek = getattr(att, "seek_offset", None) or getattr(att, "seek", None)
    if seek:
        try:
            seek(0)
        except Exception as exc:
            debug_exception("Unable to seek attachment data", exc)

    read_buffer = getattr(att, "read_buffer", None)
    if not read_buffer:
        return None

    chunks = []
    remaining = size or None
    # Read in bounded chunks so large attachments do not require a single huge
    # binding-level allocation.
    while True:
        request_size = min(65536, remaining) if remaining else 65536
        try:
            chunk = read_buffer(request_size)
        except Exception as exc:
            debug_exception("Unable to read attachment data", exc)
            return None
        if not chunk:
            break
        chunks.append(bytes(chunk))
        if remaining:
            remaining -= len(chunk)
            if remaining <= 0:
                break
    return b"".join(chunks) if chunks else None


def attachment_info(att, index):
    """Return display metadata and raw properties for an attachment."""
    name = (
        get_value(att, "long_filename", "get_long_filename")
        or get_value(att, "filename", "get_filename")
        or get_value(att, "display_name", "get_display_name")
        or record_value(att, {0x3707})
        or record_value(att, {0x3704})
        or record_value(att, {0x3001})
        or f"attachment-{index + 1}"
    )
    mime_type = (
        get_value(att, "mime_type", "get_mime_type")
        or record_value(att, {0x370E})
        or ""
    )
    size = get_value(att, "size", "get_size") or record_value(att, {0x0E20})
    try:
        size = int(size)
    except Exception:
        size = None

    return {
        "index": index,
        "name": s(name),
        "size": size,
        "type": s(mime_type),
        "properties": record_entries(att),
    }


def attachments_for(msg):
    """Return attachment metadata for all attachments on a message."""
    out = []
    for index in range(attachment_count(msg)):
        att = attachment_at(msg, index)
        if att is None:
            continue
        info = attachment_info(att, index)
        info["_object"] = att
        out.append(info)
    return out


def exposed_attachment_count(msg):
    """Return the count of attachment rows that can actually be opened."""
    return len(attachments_for(msg))


def exposed_attachment_row_count(msg):
    """Return the count of attachment rows without reading attachment metadata."""
    count = attachment_count(msg)
    rows = 0
    for index in range(count):
        if attachment_at(msg, index) is not None:
            rows += 1
    return rows

@dataclass
class MessageSnapshot:
    msg: object
    subject: str
    contact: str
    sender: str
    to: str
    cc: str
    bcc: str
    classification: str
    date: str
    html: str
    text: str
    rtf: str
    raw_headers: str
    header_rows: list
    metadata: list
    attachments: list
    attachment_indicators: list


def message_snapshot(msg, contact_cache=None):
    """Return message snapshot."""
    subject = s(get_value(msg, "subject", "get_subject")) or "(no subject)"
    contact = contact_display(msg)
    sender = resolve_sender(msg, contact_cache)
    to = resolve_to(msg, contact_cache)
    cc = resolve_cc(msg, contact_cache)
    bcc = resolve_bcc(msg, contact_cache)
    classification = email_classification(msg)
    date = s(message_date(msg))
    html, text, rtf = resolve_body(msg)
    raw_headers = transport_headers(msg)

    return MessageSnapshot(
        msg=msg,
        subject=subject,
        contact=contact,
        sender=sender,
        to=to,
        cc=cc,
        bcc=bcc,
        classification=classification,
        date=date,
        html=html,
        text=text,
        rtf=rtf,
        raw_headers=raw_headers,
        header_rows=parse_header_lines(raw_headers),
        metadata=record_entries(msg),
        attachments=attachments_for(msg),
        attachment_indicators=attachment_indicator_details(msg),
    )


def email_description_from_snapshot(snapshot):
    """Build a JSON-serializable email description from a snapshot."""
    return {
        "subject": snapshot.subject,
        "contact": snapshot.contact,
        "from": snapshot.sender,
        "to": snapshot.to,
        "cc": snapshot.cc,
        "bcc": snapshot.bcc,
        "classification": snapshot.classification,
        "date": snapshot.date,
        "headers": dict(snapshot.header_rows),
        "raw_headers": snapshot.raw_headers,
        "body": {
            "html": snapshot.html,
            "text": snapshot.text,
            "rtf": snapshot.rtf,
        },
        "attachments": [
            {
                "name": info["name"],
                "size": info["size"],
                "type": info["type"],
                "properties": [
                    serializable_record_entry(entry)
                    for entry in info["properties"]
                ],
            }
            for info in snapshot.attachments
        ],
        "attachment_indicators": snapshot.attachment_indicators,
        "metadata": [
            serializable_record_entry(entry)
            for entry in snapshot.metadata
        ],
    }


def email_description(msg):
    """Build email description."""
    return email_description_from_snapshot(message_snapshot(msg))
