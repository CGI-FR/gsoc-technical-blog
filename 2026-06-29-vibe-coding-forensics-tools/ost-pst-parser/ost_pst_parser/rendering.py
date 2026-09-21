import base64
import html as html_lib
import os
import re

from .pff_access import attachment_bytes, s


QT_SAFE_FONT_FAMILY = "Arial, Helvetica, sans-serif"
QT_FONT_REPLACEMENTS = (
    ".AppleSystemUIFont",
    ".AppleIndicFont",
    "-apple-system",
    "&#45;apple-system",
    "&#x2d;apple-system",
    "&#x2D;apple-system",
    "BlinkMacSystemFont",
    "system-ui",
    "-webkit-system-font",
)


def normalize_qt_font_css(body):
    """Normalize qt font css."""
    body = s(body)
    for font_name in QT_FONT_REPLACEMENTS:
        body = re.sub(re.escape(font_name), "Arial", body, flags=re.IGNORECASE)
    return body


def strip_html(html):
    """Strip html."""
    return re.sub("<[^<]+?>", " ", s(html or ""))


def preview_header_html(subject, sender, to, date):
    """Build preview header HTML."""
    rows = (
        ("Subject", subject),
        ("From", sender),
        ("To", to),
        ("Date", date),
    )
    body = "".join(
        "<div><b>{label}:</b> {value}</div>".format(
            label=html_lib.escape(label),
            value=html_lib.escape(s(value)),
        )
        for label, value in rows
    )
    return normalize_qt_font_css(
        f"<div style='font-family: {QT_SAFE_FONT_FAMILY}; "
        "font-size: 13px; margin: 0 0 12px 0; padding: 8px; "
        "border-bottom: 1px solid #999;'>"
        f"{body}</div>"
    )


def combine_preview_html(header, body):
    """Combine preview HTML."""
    body = normalize_qt_font_css(body)
    header = normalize_qt_font_css(header)
    match = re.search(r"<body\b[^>]*>", body, flags=re.IGNORECASE)
    if match:
        return body[:match.end()] + header + body[match.end():]
    return header + body


def renderable_html(body):
    """Return renderable html."""
    body = s(body).replace("\x00", "").strip()
    if not body:
        return ""

    if "&lt;" in body and ("<" not in body or body.find("&lt;") < body.find("<")):
        body = html_lib.unescape(body)
    body = normalize_qt_font_css(body)

    if not re.search(r"<\s*/?\s*[A-Za-z][A-Za-z0-9:_-]*(?:\s|/?>)", body):
        return ""

    return body


def rtf_to_text(body):
    """Convert an RTF body to readable plain text."""
    text = s(body).strip()
    if not text:
        return ""
    if not text.startswith("{\\rtf"):
        return text

    # RTF destinations are control-word groups whose contents are metadata or
    # binary payloads rather than visible message text.
    destinations = {
        "aftncn", "aftnsep", "aftnsepc", "annotation", "atnauthor", "atndate",
        "atnicn", "atnid", "atnparent", "atnref", "atntime", "background",
        "bkmkend", "bkmkstart", "blipuid", "buptim", "category", "colortbl",
        "comment", "company", "creatim", "datafield", "datastore", "defchp",
        "defpap", "do", "doccomm", "docvar", "dptxbxtext", "ebcend",
        "ebcstart", "factoidname", "falt", "file", "filetbl", "fldinst",
        "fontemb", "fontfile", "fonttbl", "footer", "footerf", "footerl",
        "footerr", "footnote", "formfield", "generator", "gridtbl", "header",
        "headerf", "headerl", "headerr", "hl", "hlfr", "hlinkbase", "htmltag",
        "info", "keycode", "keywords", "latentstyles", "lchars", "levelnumbers",
        "leveltext", "list", "listlevel", "listname", "listoverride",
        "listoverridetable", "listpicture", "liststylename", "listtable",
        "mmath", "nonesttables", "objalias", "objclass", "objdata", "object",
        "oldcprops", "oldpprops", "oldsprops", "oldtprops", "oleclsid",
        "operator", "panose", "password", "passwordhash", "pgp", "pgptbl",
        "picprop", "pict", "pn", "pnseclvl", "pntext", "pntxta", "pntxtb",
        "printim", "private", "propname", "protend", "protstart", "protusertbl",
        "revtbl", "revtim", "rsidtbl", "rxe", "shp", "shpgrp", "shpinst",
        "shppict", "shprslt", "shptxt", "sn", "sp", "staticval", "stylesheet",
        "subject", "sv", "svb", "tc", "template", "themedata", "title",
        "txe", "ud", "upr", "userprops", "wgrffmtfilter", "windowcaption",
        "writereservation", "writereservhash", "xe", "xmlattrname", "xmlattrvalue",
        "xmlclose", "xmlname", "xmlnstbl", "xmlopen",
    }
    special_chars = {
        "bullet": "*",
        "emdash": "-",
        "endash": "-",
        "emspace": " ",
        "enspace": " ",
        "qmspace": " ",
        "lquote": "'",
        "rquote": "'",
        "ldblquote": '"',
        "rdblquote": '"',
    }

    stack = []
    output = []
    ignorable = False
    ucskip = 1
    curskip = 0
    pattern = re.compile(
        r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)",
        re.DOTALL,
    )

    def append_plain(value):
        """Append visible plain text while honoring Unicode skip state."""
        nonlocal curskip
        if ignorable:
            return
        if curskip:
            if curskip >= len(value):
                curskip -= len(value)
                return
            value = value[curskip:]
            curskip = 0
        if value:
            output.append(value)

    # Walk control words and text in one pass, tracking group state so ignored
    # destinations do not leak into the rendered preview.
    for match in pattern.finditer(text):
        word, arg, hex_value, escaped, brace, plain = match.groups()

        if brace:
            if brace == "{":
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
            continue

        if escaped:
            if escaped == "*":
                ignorable = True
            elif escaped in "{}\\":
                append_plain(escaped)
            elif escaped == "~":
                append_plain(" ")
            elif escaped in "-_":
                append_plain("")
            continue

        if hex_value:
            if not ignorable:
                try:
                    append_plain(bytes.fromhex(hex_value).decode("cp1252"))
                except Exception:
                    pass
            continue

        if word:
            if word in destinations:
                ignorable = True
                continue
            if ignorable:
                continue
            if word == "uc":
                try:
                    ucskip = int(arg)
                except Exception:
                    ucskip = 1
            elif word == "u":
                try:
                    codepoint = int(arg)
                    if codepoint < 0:
                        codepoint += 65536
                    output.append(chr(codepoint))
                    curskip = ucskip
                except Exception:
                    pass
            elif word in ("par", "line"):
                output.append("\n")
            elif word == "tab":
                output.append("\t")
            elif word in special_chars:
                output.append(special_chars[word])
            continue

        if plain:
            append_plain(plain)

    # Collapse layout noise after the control words have been converted.
    rendered = "".join(output)
    rendered = rendered.replace("\r", "")
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def rtf_to_html(body):
    """Convert common RTF formatting to an HTML fragment."""
    text = s(body).strip()
    if not text:
        return ""
    if not text.startswith("{\\rtf"):
        return normalize_qt_font_css(
            f"<pre style='white-space: pre-wrap; font-family: {QT_SAFE_FONT_FAMILY}; font-size: 13px;'>"
            f"{html_lib.escape(text)}</pre>"
        )

    colors = parse_rtf_color_table(text)
    destinations = {
        "aftncn", "aftnsep", "aftnsepc", "annotation", "atnauthor", "atndate",
        "atnicn", "atnid", "atnparent", "atnref", "atntime", "background",
        "bkmkend", "bkmkstart", "blipuid", "buptim", "category", "colortbl",
        "comment", "company", "creatim", "datafield", "datastore", "defchp",
        "defpap", "do", "doccomm", "docvar", "dptxbxtext", "ebcend",
        "ebcstart", "factoidname", "falt", "file", "filetbl", "fldinst",
        "fontemb", "fontfile", "fonttbl", "footer", "footerf", "footerl",
        "footerr", "footnote", "formfield", "generator", "gridtbl", "header",
        "headerf", "headerl", "headerr", "hl", "hlfr", "hlinkbase", "htmltag",
        "info", "keycode", "keywords", "latentstyles", "lchars", "levelnumbers",
        "leveltext", "list", "listlevel", "listname", "listoverride",
        "listoverridetable", "listpicture", "liststylename", "listtable",
        "mmath", "nonesttables", "objalias", "objclass", "objdata", "object",
        "oldcprops", "oldpprops", "oldsprops", "oldtprops", "oleclsid",
        "operator", "panose", "password", "passwordhash", "pgp", "pgptbl",
        "picprop", "pict", "pn", "pnseclvl", "pntext", "pntxta", "pntxtb",
        "printim", "private", "propname", "protend", "protstart", "protusertbl",
        "revtbl", "revtim", "rsidtbl", "rxe", "shp", "shpgrp", "shpinst",
        "shppict", "shprslt", "shptxt", "sn", "sp", "staticval", "stylesheet",
        "subject", "sv", "svb", "tc", "template", "themedata", "title",
        "txe", "ud", "upr", "userprops", "wgrffmtfilter", "windowcaption",
        "writereservation", "writereservhash", "xe", "xmlattrname", "xmlattrvalue",
        "xmlclose", "xmlname", "xmlnstbl", "xmlopen",
    }
    special_chars = {
        "bullet": "*",
        "emdash": "-",
        "endash": "-",
        "emspace": " ",
        "enspace": " ",
        "qmspace": " ",
        "lquote": "'",
        "rquote": "'",
        "ldblquote": '"',
        "rdblquote": '"',
    }

    state = {"bold": False, "italic": False, "underline": False, "color": None, "size": None}
    stack = []
    output = []
    ignorable = False
    ucskip = 1
    curskip = 0
    pattern = re.compile(
        r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)",
        re.DOTALL,
    )

    def style_attr():
        styles = []
        if state["bold"]:
            styles.append("font-weight: 700")
        if state["italic"]:
            styles.append("font-style: italic")
        if state["underline"]:
            styles.append("text-decoration: underline")
        if state["color"] in colors:
            styles.append(f"color: {colors[state['color']]}")
        if state["size"]:
            styles.append(f"font-size: {max(7, min(72, state['size'] / 2)):.1f}pt")
        return "; ".join(styles)

    def append_text(value):
        nonlocal curskip
        if ignorable:
            return
        if curskip:
            if curskip >= len(value):
                curskip -= len(value)
                return
            value = value[curskip:]
            curskip = 0
        if not value:
            return
        escaped = html_lib.escape(value).replace("\n", "<br>")
        style = style_attr()
        if style:
            output.append(f"<span style='{style}'>{escaped}</span>")
        else:
            output.append(escaped)

    for match in pattern.finditer(text):
        word, arg, hex_value, escaped, brace, plain = match.groups()

        if brace:
            if brace == "{":
                stack.append((ucskip, ignorable, dict(state)))
            elif stack:
                ucskip, ignorable, state = stack.pop()
            continue

        if escaped:
            if escaped == "*":
                ignorable = True
            elif escaped in "{}\\":
                append_text(escaped)
            elif escaped == "~":
                append_text(" ")
            elif escaped in "-_":
                append_text("")
            continue

        if hex_value:
            if not ignorable:
                try:
                    append_text(bytes.fromhex(hex_value).decode("cp1252"))
                except Exception:
                    pass
            continue

        if word:
            if word in destinations:
                ignorable = True
                continue
            if ignorable:
                continue
            if word == "uc":
                try:
                    ucskip = int(arg)
                except Exception:
                    ucskip = 1
            elif word == "u":
                try:
                    codepoint = int(arg)
                    if codepoint < 0:
                        codepoint += 65536
                    output.append(html_lib.escape(chr(codepoint)))
                    curskip = ucskip
                except Exception:
                    pass
            elif word in ("par", "line"):
                output.append("<br>")
            elif word == "tab":
                output.append("&emsp;")
            elif word == "b":
                state["bold"] = arg != "0"
            elif word == "i":
                state["italic"] = arg != "0"
            elif word == "ul":
                state["underline"] = arg != "0"
            elif word == "ulnone":
                state["underline"] = False
            elif word == "cf":
                try:
                    state["color"] = int(arg)
                except Exception:
                    state["color"] = None
            elif word == "cf0":
                state["color"] = None
            elif word == "fs":
                try:
                    state["size"] = int(arg)
                except Exception:
                    state["size"] = None
            elif word in special_chars:
                append_text(special_chars[word])
            continue

        if plain:
            append_text(plain)

    html = "".join(output).strip()
    html = re.sub(r"(?:<br>\\s*){3,}", "<br><br>", html)
    if not html:
        return ""
    return normalize_qt_font_css(
        f"<div style='white-space: normal; font-family: {QT_SAFE_FONT_FAMILY}; font-size: 13px;'>"
        f"{html}</div>"
    )


def parse_rtf_color_table(text):
    """Return RTF color table entries keyed by 1-based color index."""
    group = extract_rtf_group(text, "colortbl")
    if not group:
        return {}
    colors = {}
    index = 0
    red = green = blue = None
    for token in re.finditer(r"\\(red|green|blue)(\d+)|;", group):
        if token.group(0) == ";":
            if red is not None and green is not None and blue is not None:
                colors[index] = f"#{red:02x}{green:02x}{blue:02x}"
            index += 1
            red = green = blue = None
            continue
        channel, value = token.group(1), max(0, min(255, int(token.group(2))))
        if channel == "red":
            red = value
        elif channel == "green":
            green = value
        elif channel == "blue":
            blue = value
    return colors


def extract_rtf_group(text, destination):
    """Extract the first RTF group for a destination control word."""
    match = re.search(r"{\\%s\b" % re.escape(destination), text)
    if not match:
        return ""
    depth = 0
    for index in range(match.start(), len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[match.start():index + 1]
    return ""

IMAGE_ATTACHMENT_EXTENSIONS = {
    ".apng", ".avif", ".bmp", ".gif", ".ico", ".jfif", ".jpeg", ".jpg",
    ".png", ".svg", ".tif", ".tiff", ".webp",
}


def image_attachment_mime(info):
    """Return image attachment mime."""
    mime_type = s(info.get("type")).split(";", 1)[0].strip().lower()
    if mime_type.startswith("image/"):
        return mime_type

    ext = os.path.splitext(s(info.get("name")))[1].lower()
    if ext not in IMAGE_ATTACHMENT_EXTENSIONS:
        return ""

    return {
        ".apng": "image/apng",
        ".avif": "image/avif",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
        ".ico": "image/x-icon",
        ".jfif": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }.get(ext, "")


def attachment_images_html(attachments):
    """Return attachment images HTML."""
    images = []
    for info in attachments:
        mime_type = image_attachment_mime(info)
        if not mime_type:
            continue

        data = attachment_bytes(info["_object"])
        if not data:
            continue

        encoded = base64.b64encode(data).decode("ascii")
        name = html_lib.escape(s(info.get("name")) or f"image-{info.get('index', len(images)) + 1}")
        images.append(
            "<figure style='margin: 16px 0 0 0; padding: 12px 0 0 0; "
            "border-top: 1px solid #777;'>"
            f"<figcaption style='font-family: {QT_SAFE_FONT_FAMILY}; font-size: 12px; "
            f"color: #777; margin-bottom: 8px;'>{name}</figcaption>"
            f"<img src='data:{mime_type};base64,{encoded}' "
            "style='display: block; max-width: 100%; height: auto;' />"
            "</figure>"
        )

    if not images:
        return ""

    return normalize_qt_font_css(
        "<section style='margin-top: 28px; padding-top: 14px; "
        "border-top: 4px solid #777;'><hr/>"
        f"<div style='font-family: {QT_SAFE_FONT_FAMILY}; font-size: 12px; font-weight: 600; "
        "letter-spacing: 0.04em; text-transform: uppercase; color: #666; "
        "margin: 0 0 12px 0;'>Attached Images Preview - Not Part of Email Body</div>"
        + "".join(images)
        + "</section>"
    )


def plain_body_html(text):
    """Return plain body HTML."""
    return normalize_qt_font_css(
        f"<pre style='white-space: pre-wrap; font-family: {QT_SAFE_FONT_FAMILY}; font-size: 13px;'>"
        f"{html_lib.escape(s(text))}</pre>"
    )


def append_html_fragment(body, fragment):
    """Append html fragment."""
    if not fragment:
        return normalize_qt_font_css(body)

    body = normalize_qt_font_css(body)
    fragment = normalize_qt_font_css(fragment)
    match = re.search(r"</body\s*>", body, flags=re.IGNORECASE)
    if match:
        return body[:match.start()] + fragment + body[match.start():]
    return body + fragment
