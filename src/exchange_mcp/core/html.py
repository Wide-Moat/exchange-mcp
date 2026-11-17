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
        self.img_tags: list[tuple[str, str]] = []

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
        if tag == "img" and "src" in a:
            # The decoded value and the source text it came from: the rewrite to cid: must act on
            # the tag as written, because the decoded form need not appear in the text at all.
            self.img_tags.append((self.get_starttag_text() or "", a["src"]))

    handle_startendtag = handle_starttag


_TAG_ATTR = re.compile(r"""([^\s/>=][^\s/>=]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]*))?[\s/]*""")


def _with_src(tag_text: str, src: str) -> str:
    """The same start tag with its first src attribute replaced, attributes walked in order so a
    value that merely contains "src=" is never mistaken for the attribute."""
    m = re.match(r"<[a-zA-Z][^\s/>]*[\s/]*", tag_text)
    if not m:
        return tag_text
    pos = m.end()
    while pos < len(tag_text) and tag_text[pos] != ">":
        a = _TAG_ATTR.match(tag_text, pos)
        if not a or a.end() == pos:
            break
        if a.group(1).lower() == "src" and a.group(2) is not None:
            return tag_text[: a.start(2)] + f'"{escape(src)}"' + tag_text[a.end(2) :]
        pos = a.end()
    return tag_text


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
    # Scanned again after inlining, which re-serialises the document.
    imgs = _Scan()
    imgs.feed(text)
    imgs.close()
    by_src: dict[str, str] = {}
    for src in dict.fromkeys(s for _, s in imgs.img_tags):
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
        if all(i.content_id != cid for i in out.inline_images):
            out.inline_images.append(InlineImage(cid, m.group(1).lower(), data))
        by_src[src] = cid
    for tag_text, src in dict.fromkeys(imgs.img_tags):
        if src in by_src and tag_text:
            text = text.replace(tag_text, _with_src(tag_text, f"cid:{by_src[src]}"))
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
    referenced = {u.strip()[4:] for u in after.img_srcs if u.strip().lower().startswith("cid:")}
    if any(i.content_id not in referenced for i in out.inline_images):
        out.errors.append("An embedded image could not be referenced by cid: and would arrive broken.")
    for url in after.img_srcs + after.hrefs:
        u = url.strip()
        scheme = u.lower()  # URL schemes are case-insensitive (RFC 3986 §3.1)
        if not u or scheme.startswith(("#", "mailto:", "tel:", "cid:")):
            continue
        if scheme.startswith("http://") and url in after.img_srcs:
            out.errors.append(f"Images must be https: {u[:200]}")
        elif not scheme.startswith(("https://", "http://")):
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


# What a browser would close implicitly when a start tag arrives: tag -> (open tags it closes,
# tags at which the search stops). Without this an unclosed hidden <p>, <li>, <td> or <head>
# would hide everything after it, while a mail client shows that text.
_SCOPE = {"applet", "caption", "html", "table", "td", "th", "marquee", "object", "template", "button"}
_CLOSES_P = {
    "address",
    "article",
    "aside",
    "blockquote",
    "center",
    "details",
    "dialog",
    "dir",
    "div",
    "dl",
    "dd",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "hr",
    "li",
    "main",
    "menu",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "summary",
    "table",
    "ul",
}
_IMPLIED: dict[str, tuple[set[str], set[str]]] = {
    "li": ({"li"}, _SCOPE | {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, _SCOPE | {"dl"}),
    "dd": ({"dt", "dd"}, _SCOPE | {"dl"}),
    "tr": ({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot", "html"}),
    "td": ({"td", "th"}, {"tr", "table", "html"}),
    "th": ({"td", "th"}, {"tr", "table", "html"}),
    "thead": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table", "html"}),
    "tbody": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table", "html"}),
    "tfoot": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table", "html"}),
    "option": ({"option"}, {"select", "datalist", "optgroup", "html"}),
    "optgroup": ({"option", "optgroup"}, {"select", "html"}),
}
# What may sit inside <head>; any other start tag means the body has begun.
_HEAD_CONTENT = {"html", "head", "title", "meta", "link", "style", "script", "base", "noscript", "template"}


def _decls(style: str) -> dict[str, tuple[str, bool]]:
    """property -> (value, !important), lower-cased."""
    out: dict[str, tuple[str, bool]] = {}
    for decl in style.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            v = v.strip().lower()
            important = bool(re.search(r"!\s*important", v))
            out[k.strip().lower()] = (re.sub(r"!\s*important", "", v).strip(), important)
    return out


def _is_transparent(c: str) -> bool:
    c = c.strip().lower()
    if c == "transparent":
        return True
    m = re.match(r"(?:rgba|hsla)\(.*[,/]\s*(0*\.?0*%?)\s*\)$", c)
    return bool(m and _is_zero(m.group(1).rstrip("%")))


def _norm_color(c: str) -> str:
    c = c.strip().lower()
    named = {"white": "#ffffff", "black": "#000000", "#fff": "#ffffff", "#000": "#000000"}
    if c in named:
        return named[c]
    m = re.match(r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)", c)
    if m:
        r, g, b = (int(x) for x in m.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.fullmatch(r"#[0-9a-f]{3}", c):
        return "#" + "".join(ch * 2 for ch in c[1:])
    return c


_BG_WORDS = {
    "none",
    "repeat",
    "no-repeat",
    "repeat-x",
    "repeat-y",
    "space",
    "round",
    "center",
    "top",
    "bottom",
    "left",
    "right",
    "fixed",
    "scroll",
    "local",
    "cover",
    "contain",
    "auto",
    "border-box",
    "padding-box",
    "content-box",
}


def _bg_color(v: str) -> str:
    """The colour inside a `background` shorthand; the shorthand without one resets it."""
    v = re.sub(r"url\([^)]*\)", " ", v)
    m = re.search(r"#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)", v)
    if m:
        return m.group(0)
    for word in re.split(r"[\s/]+", v):
        if re.fullmatch(r"[a-z]+", word) and word not in _BG_WORDS:
            return word
    return "transparent"


def _is_zero(v: str) -> bool:
    m = re.match(r"^\s*0*(\.0*)?\s*(px|pt|em|rem|%)?\s*$", v)
    return bool(m)


def _hides(s: dict[str, str]) -> bool:
    """Declarations that make an element and everything inside it invisible. (font-size and colour
    can be overridden by a descendant, so they are tracked per element instead.)"""
    if s.get("display") == "none" or s.get("visibility") in ("hidden", "collapse"):
        return True
    if "opacity" in s and _is_zero(s["opacity"]):
        return True
    for k in ("max-height", "max-width"):
        if k in s and _is_zero(s[k]):
            return True
    return s.get("overflow") == "hidden" and any(k in s and _is_zero(s[k]) for k in ("height", "width"))


# ---- <style> blocks ------------------------------------------------------------------
#
# A deliberately small CSS engine, enough to see what a stylesheet hides. It evaluates exactly:
# type, .class, #id, * and their compounds (div.a#b), joined by descendant or child (>)
# combinators, comma lists, !important, specificity and source order, inline style on top, and
# @media blocks whose query is empty, `all` or `screen`.
#
# It cannot evaluate attribute selectors, structural pseudo-classes (:first-child, :not(),
# :nth-*) or sibling combinators (+, ~). For those it is conservative in the direction that
# matters for injected text, without dropping visible text wholesale: the unsupported parts are
# removed and the rule applies only when what remains of the rightmost compound names a class or
# an id (`.promo:first-child` -> `.promo`). One that leaves just a tag (`p:first-child`,
# `td + td`) is ignored, because applying it would hide every <p> or <td> in the message.
# Rules that cannot hide the element's own text are skipped: dynamic pseudo-classes (:hover,
# :focus, …: the resting state is what is read), pseudo-elements (::before), and @media with a
# feature condition, `print` or `not` (responsive rules that hide on one screen size only).
# Selectors with escapes or namespaces are ignored.

_RELEVANT = {
    "display",
    "visibility",
    "opacity",
    "font-size",
    "max-height",
    "max-width",
    "height",
    "width",
    "overflow",
    "color",
    "background",
    "background-color",
}
_DYNAMIC = {
    "hover",
    "focus",
    "focus-within",
    "focus-visible",
    "active",
    "visited",
    "link",
    "target",
    "checked",
    "indeterminate",
    "placeholder-shown",
    "autofill",
}
_PSEUDO_ELEMENTS = {"before", "after", "first-line", "first-letter", "marker", "placeholder", "selection", "backdrop"}
_COMPOUND = re.compile(r"(\*|[a-z][a-z0-9-]*)?((?:[#.][a-z_][\w-]*)*)", re.I)


@dataclass(frozen=True)
class _Compound:
    tag: str | None
    ident: str | None
    classes: frozenset[str]

    def matches(self, tag: str, ident: str | None, classes: frozenset[str]) -> bool:
        return (
            (self.tag is None or self.tag == tag)
            and (self.ident is None or self.ident == ident)
            and self.classes <= classes
        )


@dataclass(frozen=True)
class _Rule:
    chain: tuple[tuple[str, _Compound], ...]  # (combinator to the left, compound), subject last
    key: tuple[tuple[int, int, int], int]  # specificity, source order
    decls: dict[str, tuple[str, bool]]


def _compound(text: str) -> _Compound | None:
    m = _COMPOUND.fullmatch(text)
    if not m:
        return None
    tag = m.group(1).lower() if m.group(1) and m.group(1) != "*" else None
    parts = re.findall(r"[#.][^#.]+", m.group(2))
    ids = [p[1:] for p in parts if p[0] == "#"]
    if len(set(ids)) > 1:
        return None
    return _Compound(tag, ids[0] if ids else None, frozenset(p[1:] for p in parts if p[0] == "."))


def _split_top(text: str, sep: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _rule(selector: str, order: int, decls: dict[str, tuple[str, bool]]) -> _Rule | None:
    sel = selector.strip()
    if not sel or "\\" in sel or "|" in sel or "::" in sel:
        return None
    pseudo = [p.lower() for p in re.findall(r":([\w-]+)", sel)]
    if any(p in _DYNAMIC or p in _PSEUDO_ELEMENTS for p in pseudo):
        return None
    spec = (
        sel.count("#"),
        sel.count(".") + sel.count("[") + len(pseudo),
        len(re.findall(r"(?:^|[\s>+~])[a-z]", sel, re.I)),
    )
    exact = not re.search(r"[\[:+~]", sel)
    sel = re.sub(r"\[[^\]]*\]", "", sel)
    sel = re.sub(r":[\w-]+(?:\([^)]*\))?", "", sel)
    tokens = re.sub(r"\s*([>+~])\s*", r" \1 ", sel).split()
    chain: list[tuple[str, _Compound]] = []
    comb = ""
    for tok in tokens:
        if tok in (">", "+", "~"):
            comb = tok
            continue
        c = _compound(tok)
        if c is None:
            return None
        chain.append((comb or (" " if chain else ""), c))
        comb = ""
    if not chain:
        return None
    if not exact:
        subject = chain[-1][1]
        if subject.ident is None and not subject.classes:
            return None
        chain = [("", subject)]
    return _Rule(tuple(chain), (spec, order), decls)


def _media_applies(prelude: str) -> bool:
    m = re.match(r"@media\b(.*)$", prelude, re.I | re.S)
    if not m:
        return False  # @supports, @font-face, @keyframes, …
    query = m.group(1).strip().lower()
    if not query:
        return True
    for part in query.split(","):
        words = [w for w in part.split() if w != "only"]
        if "(" not in part and "not" not in words and words in (["all"], ["screen"]):
            return True
    return False


def _parse_css(css: str, rules: list[_Rule]) -> None:
    i = 0
    while True:
        brace = css.find("{", i)
        if brace < 0:
            return
        depth, end = 0, len(css)
        for j in range(brace, len(css)):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        prelude = css[i:brace].rsplit(";", 1)[-1].strip()  # drops @import …; and stray text
        body = css[brace + 1 : end]
        if prelude.startswith("@"):
            if _media_applies(prelude):
                _parse_css(body, rules)
        else:
            decls = {k: v for k, v in _decls(body).items() if k in _RELEVANT}
            if decls:
                for sel in _split_top(prelude, ","):
                    r = _rule(sel, len(rules), decls)
                    if r:
                        rules.append(r)
        i = end + 1


class _Styles(HTMLParser):
    """Collects every <style> block: a rule applies to elements before the block too."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.css: list[str] = []
        self._in = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in = tag == "style"
        if self._in:
            self.css.append("")

    def handle_endtag(self, tag: str) -> None:
        self._in = False

    def handle_data(self, data: str) -> None:
        if self._in:
            self.css[-1] += data


def _stylesheet(html: str) -> list[_Rule]:
    p = _Styles()
    p.feed(html)
    p.close()
    css = re.sub(r"/\*.*?\*/", "", "\n".join(p.css), flags=re.S).replace("<!--", "").replace("-->", "")
    rules: list[_Rule] = []
    _parse_css(css, rules)
    return rules


@dataclass
class _Frame:
    tag: str
    ident: str | None
    classes: frozenset[str]
    hidden: bool  # this element or an ancestor hides everything inside
    color: str | None  # effective (inherited) text colour
    bg: str | None  # nearest background painted behind this element
    tiny: bool  # effective font-size is zero
    href: str | None = None

    @property
    def text_hidden(self) -> bool:
        if self.hidden or self.tiny:
            return True
        if self.color is None:
            return False
        return _is_transparent(self.color) or (self.bg is not None and _norm_color(self.color) == _norm_color(self.bg))


def _ancestors_match(chain: tuple[tuple[str, _Compound], ...], i: int, anc: list[_Frame], end: int) -> bool:
    """Does chain[:i] match among anc[:end], given chain[i] matched the element after anc[end-1]?"""
    if i == 0:
        return True
    comb, comp = chain[i][0], chain[i - 1][1]
    if comp.tag in ("html", "body") and comp.ident is None and not comp.classes:
        # A fragment has no <html> or <body> element, but its content is inside both.
        if not any(f.tag == comp.tag for f in anc[:end]):
            return _ancestors_match(chain, i - 1, anc, end)
    if comb == ">":
        f = anc[end - 1] if end else None
        return bool(f and comp.matches(f.tag, f.ident, f.classes) and _ancestors_match(chain, i - 1, anc, end - 1))
    return any(
        comp.matches(anc[j].tag, anc[j].ident, anc[j].classes) and _ancestors_match(chain, i - 1, anc, j)
        for j in range(end - 1, -1, -1)
    )


class _ToText(HTMLParser):
    def __init__(self, rules: list[_Rule]) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[_Frame] = []
        # Rules bucketed by one thing their subject requires, so an element only meets candidates.
        self.rules: dict[tuple[str, str], list[_Rule]] = {}
        for r in rules:
            c = r.chain[-1][1]
            key = ("#", c.ident) if c.ident else (".", min(c.classes)) if c.classes else ("", c.tag or "*")
            self.rules.setdefault(key, []).append(r)

    @property
    def hidden(self) -> bool:
        return bool(self.stack) and self.stack[-1].hidden

    def _computed(self, tag: str, ident: str | None, classes: frozenset[str], style: str) -> dict[str, str]:
        entries: list[tuple[tuple, str, str]] = []
        keys = [("", tag), ("", "*"), *((".", c) for c in classes)] + ([("#", ident)] if ident else [])
        for r in (r for k in keys for r in self.rules.get(k, ())):
            if r.chain[-1][1].matches(tag, ident, classes) and _ancestors_match(
                r.chain, len(r.chain) - 1, self.stack, len(self.stack)
            ):
                entries.extend(((imp, 0, *r.key), k, v) for k, (v, imp) in r.decls.items())
        entries.extend(((imp, 1, (0, 0, 0), 0), k, v) for k, (v, imp) in _decls(style).items())
        out: dict[str, str] = {}
        for _, k, v in sorted(entries, key=lambda e: e[0]):
            out[k] = v
            if k == "background":
                out["background-color"] = _bg_color(v)
        return out

    def _pop(self) -> None:
        f = self.stack.pop()
        if f.hidden:
            return
        if f.href and f.href.lower().startswith(("http://", "https://")) and not f.text_hidden:
            text_tail = self.out[-1] if self.out else ""
            if f.href not in text_tail:
                self.out.append(f" ({f.href})")
        if f.tag in _BLOCK:
            self.out.append("\n")

    def _pop_to(self, idx: int) -> None:
        while len(self.stack) > idx:
            self._pop()

    def _find(self, tags: set[str], boundary: set[str]) -> int | None:
        """The outermost open element among `tags` below the nearest boundary: a new <tr> closes
        the open <tr>, not just the <td> inside it."""
        found = None
        for i in range(len(self.stack) - 1, -1, -1):
            t = self.stack[i].tag
            if t in boundary:
                break
            if t in tags:
                found = i
        return found

    def _close_implied(self, tag: str) -> None:
        if tag not in _HEAD_CONTENT:
            idx = self._find({"head"}, set())
            if idx is not None:
                self._pop_to(idx)
        if tag in _CLOSES_P:
            idx = self._find({"p"}, _SCOPE)
            if idx is not None:
                self._pop_to(idx)
        if tag in _IMPLIED:
            closes, boundary = _IMPLIED[tag]
            idx = self._find(closes, boundary)
            if idx is not None:
                self._pop_to(idx)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        a = {k.lower(): (v or "") for k, v in attrs}
        ident = a.get("id") or None
        classes = frozenset(a.get("class", "").split())
        s = self._computed(tag, ident, classes, a.get("style", ""))
        own_hidden = tag in _DROP or "hidden" in a or a.get("aria-hidden", "").lower() == "true" or _hides(s)
        parent = self.stack[-1] if self.stack else None
        if tag in _VOID:
            if self.hidden or own_hidden or ("font-size" in s and _is_zero(s["font-size"])):
                return
            if tag == "img":
                w, h = a.get("width", ""), a.get("height", "")
                tiny = w.strip("px ") in ("0", "1") and h.strip("px ") in ("0", "1")
                if not tiny and a.get("alt", "").strip():
                    self.out.append(f"[image: {a['alt'].strip()}]")
            elif tag in ("br", "hr"):
                self.out.append("\n")
            return
        color = s.get("color") or a.get("color") or ""
        if not color or color in ("inherit", "currentcolor", "initial", "unset"):
            color = parent.color if parent else None
        bg = s.get("background-color") or a.get("bgcolor") or ""
        if not bg or bg in ("inherit", "initial", "unset", "currentcolor") or _is_transparent(bg):
            bg = parent.bg if parent else None
        fs = s.get("font-size")
        if fs is None or fs in ("inherit", "unset") or (re.search(r"(em|%)\s*$", fs) and not _is_zero(fs)):
            tiny = parent.tiny if parent else False  # relative to a zero size stays zero
        else:
            tiny = _is_zero(fs)
        frame = _Frame(
            tag,
            ident,
            classes,
            hidden=(parent.hidden if parent else False) or own_hidden,
            color=color,
            bg=bg,
            tiny=tiny,
            href=a.get("href") if tag == "a" else None,
        )
        self.stack.append(frame)
        if frame.hidden:
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag in ("td", "th"):
            self.out.append(" | ")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i].tag == tag), None)
        if idx is not None:
            self._pop_to(idx)

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1].text_hidden:
            return
        self.out.append(re.sub(r"[ \t\r\f\v]+", " ", data))


def html_to_text(html: str) -> str:
    p = _ToText(_stylesheet(html))
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
