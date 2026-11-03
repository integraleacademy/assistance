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


_SIGNATURE_BLOCK = re.compile(r"<!-- EMAIL_SIGNATURE_START -->.*?<!-- EMAIL_SIGNATURE_END -->", re.S)
_DIRECTOR_SIGNATURE = re.compile(
    r"<p\b[^>]*>\s*<(?:strong|b)\b[^>]*>\s*Clément\s+VAILLANT\s*</(?:strong|b)>\s*"
    r"<br\s*/?>\s*Directeur\b(?:(?!</p>).)*</p>", re.I | re.S,
)
_LOGO_SRC = re.compile(r"""(\ssrc\s*=\s*)(["'])(.*?)\2""", re.I | re.S)


def brand_saved_template(body, logo_url, signature, avatar, *, saved):
    """Normalize copies for display/delivery, without rewriting saved versions."""
    def logo_image(match):
        image = match.group()
        src = _LOGO_SRC.search(image)
        if not src:
            return image
        url = src[3]
        if (url in ("/static/logo.png", "cid:logo_cid", "cid:integrale-academy-logo", logo_url)
                or "integraleacademy.file.force.com/file-asset-public/Logo_" in url):
            return image[:src.start()] + f'{src[1]}"{logo_url}"' + image[src.end():]
        return image

    body = _IMAGE.sub(logo_image, body)
    if saved:
        body = _SIGNATURE_BLOCK.sub(lambda _: signature, body)
        body = _DIRECTOR_SIGNATURE.sub(lambda _: signature, body)
    body = with_cassandre_memoji(body, avatar)
    if saved and re.search(r"<(?:!doctype|html)\b", body, re.I):
        if not any(logo_url in image.group() for image in _IMAGE.finditer(body)):
            logo = f'<div style="text-align:center;padding:16px"><img src="{logo_url}" width="105" alt="Intégrale Academy"></div>'
            opening = re.search(r"<body\b[^>]*>", body, re.I) or re.search(r"</head>|<html\b[^>]*>", body, re.I)
            at = opening.end() if opening else 0
            body = body[:at] + logo + body[at:]
        if signature not in body and avatar not in body:
            closing = re.search(r"</body\s*>|</html\s*>", body, re.I)
            at = closing.start() if closing else len(body)
            body = body[:at] + signature + body[at:]
    return body
