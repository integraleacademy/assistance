"""Refresh Cassandre's saved HTML signatures without changing stored templates."""

import html
import re


_SPACE = r"(?:\s|&nbsp;|&#160;|&#x0*a0;)+"
_NAME = rf"Cassandre(?:{_SPACE}MENARD)?"
_NAME_ELEMENT = re.compile(
    rf"<(?P<tag>strong|b|span)\b[^>]*>\s*{_NAME}\s*</(?P=tag)>"
    rf"|<(?:p|div)\b[^>]*>\s*(?P<plain_name>{_NAME})(?=\s*<br\b)", re.I,
)
_INITIALS = re.compile(r"<div\b[^>]*>\s*CM\s*</div>", re.I)
_IMAGE = re.compile(r"<img\b[^>]*>", re.I)


def with_cassandre_memoji(body, avatar):
    """Update actual signatures, preserving the rest of the original HTML.

    Saved CRM models contain complete HTML, so changing the wrapper alone
    cannot update them. Match the name followed by her role/contact details,
    never a mention of Cassandre in the message itself. Repeated previews and
    sends are idempotent, and historical messages on disk stay untouched.
    """
    body = str(body or "")
    if "cassandre" not in body.casefold():
        return body
    edits = []
    for name in _NAME_ELEMENT.finditer(body):
        start = name.start("plain_name") if name.group("plain_name") else name.start()
        following = re.split(r"</(?:td|p|table)>|</body>", body[name.end():], 1, flags=re.I)[0]
        details = html.unescape(re.sub(r"<[^>]+>", " ", following))
        if not re.search(r"responsable\s+commerciale|cassandre@integraleacademy\.com", details, re.I):
            continue
        # In the standard layout the preceding cell contains the avatar.
        # Restrict replacements to this signature row/paragraph, not the body.
        prefix = body[:start]
        boundary = max(
            (match.end() for match in re.finditer(r"<(?:tr|p)\b[^>]*>|<!-- EMAIL_SIGNATURE_START -->", prefix, re.I)),
            default=start,
        )
        before = body[boundary:start]
        if avatar in before:
            continue
        initials = list(_INITIALS.finditer(before))
        portraits = [
            image for image in _IMAGE.finditer(before)
            if re.search(r"cassandre|memoji", image.group(), re.I)
        ]
        old_avatar = portraits[-1] if portraits else (initials[-1] if initials else None)
        if old_avatar:
            edits.append((boundary + old_avatar.start(), boundary + old_avatar.end(), avatar))
        else:
            edits.append((start, start, avatar + '<br>'))
    for start, end, replacement in reversed(edits):
        body = body[:start] + replacement + body[end:]
    return body
