"""HTML in both directions.

Outgoing: whatever the model wrote must survive Outlook, other mail clients and phones and carry nothing
active. Incoming: whatever someone else wrote must reach the model as plain text, without the
parts a reader would never see: that is where injected instructions hide.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

import css_inline
import nh3

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 3 * 1024 * 1024

_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "center",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "font",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "ins",
    "li",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ATTRS = {
    "*": {"style", "align", "valign", "width", "height", "bgcolor", "dir", "lang", "title"},
    "a": {"href", "name", "target"},
    "img": {"src", "alt", "border"},
    "table": {"border", "cellpadding", "cellspacing", "role"},
    "td": {"colspan", "rowspan", "nowrap"},
    "th": {"colspan", "rowspan", "scope"},
    "font": {"color", "face", "size"},
    "col": {"span"},
    "ol": {"start", "type"},
}
_SCHEMES = {"http", "https", "mailto", "tel", "cid"}
_FORBIDDEN_TAGS = {
    "script",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "link",
    "meta",
    "base",
    "frame",
    "frameset",
    "applet",
    "svg",
    "math",
    "style",
}

_DATA_IMG = re.compile(r"^data:(image/(?:png|jpeg|gif|webp|bmp));base64,([A-Za-z0-9+/=\s]+)$", re.I)


@dataclass
class InlineImage:
    content_id: str
    content_type: str
    data: bytes


@dataclass
class Outgoing:
    html: str
    inline_images: list[InlineImage] = field(default_factory=list)
    sanitized: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.html.encode()) + sum(len(i.data) for i in self.inline_images)

    def report(self) -> dict:
        r: dict = {"size": self.size}
        if self.sanitized:
            r["sanitized"] = self.sanitized
        if self.warnings:
            r["warnings"] = self.warnings
        return r


class _Scan(HTMLParser):
    """Collects what the sanitiser will remove, image sources, and layout constructs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.removed: dict[str, int] = {}
        self.img_srcs: list[str] = []
        self.hrefs: list[str] = []
        self.imgs_without_alt = 0
        self.styles: list[str] = []

    def _note(self, what: str) -> None:
        self.removed[what] = self.removed.get(what, 0) + 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _FORBIDDEN_TAGS and tag != "style":
            self._note(f"<{tag}>")
        a = {k.lower(): (v or "") for k, v in attrs}
        for k, v in a.items():
            if k.startswith("on"):
                self._note(f"{k} attribute")
            if k in ("href", "src", "action", "formaction") and re.match(r"\s*(javascript|vbscript):", v, re.I):
                self._note(f"{v.split(':')[0].strip().lower()}: URL")
        if "style" in a:
            self.styles.append(a["style"])
        if tag == "img":
            self.img_srcs.append(a.get("src", ""))
            if not a.get("alt"):
                self.imgs_without_alt += 1
        if tag == "a" and "href" in a:
            self.hrefs.append(a["href"])

    handle_startendtag = handle_starttag


def _inline_css(html: str) -> str:
    inliner = css_inline.CSSInliner(load_remote_stylesheets=False, keep_style_tags=False, keep_link_tags=False)
    return inliner.inline(html)


def prepare_outgoing(raw: bytes | str, *, max_bytes: int = MAX_HTML_BYTES) -> Outgoing:
    """decode → size → inline CSS → images to cid → sanitise → validate."""
    out = Outgoing(html="")
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            out.errors.append(f"The HTML is {len(raw)} bytes; the limit is {max_bytes}.")
            return out
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            out.errors.append("The HTML must be UTF-8.")
            return out
    else:
        text = raw
        if len(text.encode()) > max_bytes:
            out.errors.append(f"The HTML is {len(text.encode())} bytes; the limit is {max_bytes}.")
            return out

    before = _Scan()
    before.feed(text)

    if re.search(r"<style[\s>]", text, re.I):
        text = _inline_css(text)
        out.sanitized.append("<style> rules moved into style attributes")

    # data: images become inline attachments referenced by cid:, because Outlook blocks them.
    for src in dict.fromkeys(before.img_srcs):
        m = _DATA_IMG.match(src.strip())
        if not m:
            continue
        try:
            data = base64.b64decode(re.sub(r"\s", "", m.group(2)), validate=True)
        except (binascii.Error, ValueError):
            out.errors.append("An embedded image is not valid base64.")
            continue
        if len(data) > MAX_INLINE_IMAGE_BYTES:
            out.errors.append(f"An embedded image is {len(data)} bytes; the limit is {MAX_INLINE_IMAGE_BYTES}.")
            continue
        cid = "img-" + hashlib.sha256(data).hexdigest()[:16]
        out.inline_images.append(InlineImage(cid, m.group(1).lower(), data))
        text = text.replace(src, f"cid:{cid}")
    if out.inline_images:
        out.sanitized.append(f"{len(out.inline_images)} embedded image(s) attached inline and referenced by cid:")

    clean = nh3.clean(
        text,
        tags=_TAGS,
        attributes=_ATTRS,
        url_schemes=_SCHEMES,
        link_rel=None,
        strip_comments=True,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math"},
    )
    out.sanitized.extend(f"removed {what}" + (f" ×{n}" if n > 1 else "") for what, n in sorted(before.removed.items()))

    after = _Scan()
    after.feed(clean)
    for url in after.img_srcs + after.hrefs:
        u = url.strip()
        if not u or u.startswith(("#", "mailto:", "tel:", "cid:")):
            continue
        if u.startswith("http://") and url in after.img_srcs:
            out.errors.append(f"Images must be https: {u[:200]}")
        elif not u.startswith(("https://", "http://")):
            out.errors.append(f"Links and images must be absolute https URLs: {u[:200]}")
    style = " ".join(after.styles).lower()
    for needle, why in (
        ("display:flex", "flex layout"),
        ("display: flex", "flex layout"),
        ("display:grid", "grid layout"),
        ("display: grid", "grid layout"),
        ("position:", "CSS positioning"),
        ("background-image", "background images"),
        ("@font-face", "web fonts"),
    ):
        if needle in style and f"{why} does not render in Outlook for Windows" not in out.warnings:
            out.warnings.append(f"{why} does not render in Outlook for Windows")
    if after.imgs_without_alt:
        out.warnings.append(f"{after.imgs_without_alt} image(s) without alt text")
    for w in re.findall(r"width\s*:\s*(\d+)px", style):
        if int(w) > 800:
            out.warnings.append("content wider than 800px will scroll on phones")
            break
    if len(after.img_srcs) > 30:
        out.warnings.append("more than 30 images; many clients will clip or block them")
    out.html = clean
    return out


def text_to_html(text: str) -> str:
    paras = [p for p in re.split(r"\n{2,}", text.strip()) if p]
    return "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paras) or "<p></p>"


# ---------------------------------------------------------------------------------------
# Incoming: HTML to text, without what a reader would not see.

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK = {
    "p",
    "div",
    "br",
    "tr",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "hr",
    "table",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "section",
    "article",
    "header",
    "footer",
}
_DROP = {"script", "style", "head", "title", "noscript", "template", "svg", "math", "object", "iframe"}


def _style_map(style: str) -> dict[str, str]:
    out = {}
    for decl in style.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def _norm_color(c: str) -> str:
    c = c.strip().lower()
    named = {"white": "#ffffff", "black": "#000000", "#fff": "#ffffff", "#000": "#000000"}
    if c in named:
        return named[c]
    m = re.match(r"rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)", c)
    if m:
        r, g, b = (int(x) for x in m.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.fullmatch(r"#[0-9a-f]{3}", c):
        return "#" + "".join(ch * 2 for ch in c[1:])
    return c


def _hidden(tag: str, a: dict[str, str], inherited_bg: str | None) -> bool:
    if "hidden" in a or a.get("aria-hidden", "").lower() == "true":
        return True
    s = _style_map(a.get("style", ""))
    if s.get("display") == "none" or s.get("visibility") in ("hidden", "collapse"):
        return True
    if "opacity" in s and _is_zero(s["opacity"]):
        return True
    if "font-size" in s and _is_zero(s["font-size"]):
        return True
    for k in ("max-height", "max-width"):
        if k in s and _is_zero(s[k]):
            return True
    if s.get("overflow") == "hidden" and any(k in s and _is_zero(s[k]) for k in ("height", "width")):
        return True
    color = s.get("color") or a.get("color")
    bg = s.get("background-color") or s.get("background") or a.get("bgcolor") or inherited_bg
    return bool(color and bg and _norm_color(color) == _norm_color(bg))


def _is_zero(v: str) -> bool:
    m = re.match(r"^\s*0*(\.0*)?\s*(px|pt|em|rem|%)?\s*$", v)
    return bool(m)


class _ToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, bool, str | None]] = []  # tag, hidden, background
        self.link: list[str | None] = []
        self.cell = False

    @property
    def hidden(self) -> bool:
        return any(h for _, h, _ in self.stack)

    @property
    def bg(self) -> str | None:
        for _, _, b in reversed(self.stack):
            if b:
                return b
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        s = _style_map(a.get("style", ""))
        bg = s.get("background-color") or s.get("background") or a.get("bgcolor") or None
        hid = tag in _DROP or _hidden(tag, a, self.bg)
        if tag in _VOID:
            if self.hidden or hid:
                return
            if tag == "img":
                w, h = a.get("width", ""), a.get("height", "")
                tiny = w.strip("px ") in ("0", "1") and h.strip("px ") in ("0", "1")
                if not tiny and a.get("alt", "").strip():
                    self.out.append(f"[image: {a['alt'].strip()}]")
            elif tag in ("br", "hr"):
                self.out.append("\n")
            return
        self.stack.append((tag, hid, bg))
        if self.hidden:
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "a":
            self.link.append(a.get("href"))
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        was_hidden = self.hidden
        del self.stack[idx:]
        if was_hidden and self.hidden:
            return
        if tag == "a" and self.link:
            href = self.link.pop()
            if href and href.startswith(("http://", "https://")) and not was_hidden:
                text_tail = self.out[-1] if self.out else ""
                if href not in text_tail:
                    self.out.append(f" ({href})")
        if tag in _BLOCK and not was_hidden:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        self.out.append(re.sub(r"[ \t\r\f\v]+", " ", data))


def html_to_text(html: str) -> str:
    p = _ToText()
    p.feed(html)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cap(text: str, max_chars: int) -> tuple[str, dict]:
    if len(text) <= max_chars:
        return text, {}
    return text[:max_chars], {"truncated": True, "original_chars": len(text)}
