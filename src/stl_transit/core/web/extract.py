"""HTML normalization and structured extraction.

Pure functions over HTML strings. Nothing here fetches, stores, prints, or
exits (spec 2.1): `service.py` does the fetching and the storing, this module
does the analysis.

Parsed with the stdlib `html.parser`. BeautifulSoup and lxml are both
deliberately absent (spec 7): this reads five pages on a public marketing site,
and a C-extension parser would cost more to install, pin, and defend in a
vetting review than the ~120 lines of tree building below.

Metro's markup is not a contract. It is a CMS theme that will be restyled
without notice, so every extractor tries several strategies in order and reports
which one matched. The reader who has to fix this in eighteen months needs to
know whether one selector broke or the whole page shape moved -- and the answer
must be in the result, not in a stack trace.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from typing import Any, Iterator

from ...errors import ExtractionFailed, UsageError

# Extractor names. These are the legal values of `extractor` in sources.toml;
# a name outside this set is a config typo, not a page redesign (see `extract`).
EXTRACTORS = ("fare_table", "holiday_table", "pick_id", "alert_list", "text")

# Dropped wholesale before any text is read. script/style/noscript carry the
# analytics IDs and CSRF nonces that change on every request; svg is a wall of
# path data with no readable content; head carries meta tags whose only job is
# to be different every time.
DROP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "head", "template", "iframe", "object", "canvas"}
)

# Site chrome. Dropped only when isolating main content (`main_text`), never by
# `normalize`, which stays total so a caller can hash whatever it was handed.
CHROME_TAGS = frozenset({"nav", "aside", "footer"})

# Inline elements do not start a new line of text. Everything else does, which
# is what makes the normalized form line-oriented -- and a line-oriented diff is
# the difference between `web diff` being readable and being one 40 KB line.
INLINE_TAGS = frozenset(
    {
        "a", "abbr", "acronym", "b", "bdi", "bdo", "big", "cite", "code", "del",
        "dfn", "em", "font", "i", "img", "ins", "kbd", "mark", "nobr", "q", "s",
        "samp", "small", "span", "strike", "strong", "sub", "sup", "time", "tt",
        "u", "var", "wbr",
    }
)

VOID_TAGS = frozenset(
    {
        "area", "base", "basefont", "br", "col", "embed", "hr", "img", "input",
        "keygen", "link", "meta", "param", "source", "track", "wbr",
    }
)

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

LEAF_BLOCK_TAGS = frozenset(
    {"p", "li", "dd", "dt", "td", "th", "div", "figcaption", "blockquote", "caption"}
)

# Tags whose appearance implicitly closes an open sibling. Metro's pages are
# hand-edited in a CMS, where an unclosed <li> or <td> is routine; a strict
# stack would nest the rest of the table inside the first cell and every
# extractor downstream would see one enormous row.
_IMPLIED_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
    "thead": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tfoot", "tr", "td", "th"},
}

_WS = re.compile(r"\s+")
# Zero-width space/non-joiner/joiner/word-joiner/BOM. Invisible in a browser,
# and a theme update that sprinkles them through a heading would otherwise read
# as a content change.
_ZERO_WIDTH = re.compile(
    "[" + "".join(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)) + "]"
)

# Everything that changes without the page changing. Each pattern is here
# because leaving it in makes `web check` fire on every run, and a check that
# always fires is a check nobody reads.
NOISE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # Cache-busting query strings on assets: ?ver=6.4.3, ?_=1754236800000.
    ("cache_buster",
     re.compile(r"[?&](?:v|ver|version|cb|cache|rev|nocache|_|t|ts)=[A-Za-z0-9._%-]+", re.I),
     ""),
    ("uuid",
     re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
     "[nonce]"),
    # 16+ hex characters: build hashes, CSP nonces, session ids. No English word
    # is sixteen letters drawn only from a-f.
    ("hex_digest", re.compile(r"\b[0-9a-fA-F]{16,}\b"), "[nonce]"),
    # Machine timestamps only -- the T separator is required. "September 7, 2026"
    # and "10:30 PM" are content on these pages and must survive.
    ("iso_timestamp",
     re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
     "[timestamp]"),
    # Millisecond epochs. Deliberately not 10-digit epochs: a 10-digit number is
    # also a phone number, and Metro prints (314) 982-1400 on half these pages.
    ("epoch_ms", re.compile(r"\b1[0-9]{12}\b"), "[timestamp]"),
    # Long mixed-case-and-digit tokens: analytics client ids, session tokens.
    ("opaque_token",
     re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}\b"),
     "[nonce]"),
)

# Bounds. An MCP client is a context window (spec 2.4), and a page redesign that
# turned one table into four thousand rows must not be a denial of service
# against the conversation that noticed it.
MAX_ROWS = 500
MAX_ALERTS = 100
MAX_TEXT_CHARS = 20_000
MAX_ALERT_BODY_CHARS = 2_000
MAX_URLS_PER_ID = 10
MAX_URL_SAMPLE = 20

MONEY = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
# Anchored: a whole cell reading "Free" is a price. "free transfer" in a notes
# column is not, and treating it as one would publish a $0.00 adult fare.
FREE_CELL = re.compile(r"^\s*(free|no charge|no cost|n/?a)\s*$", re.I)
FREE_SUFFIX = re.compile(r"[:\-–—]\s*(free|no charge|no cost)\s*$", re.I)

# Column headers that are already notes; prefixing their values with the header
# name would produce "Notes: Notes: transfers included".
GENERIC_NOTE_HEADERS = re.compile(r"^\s*(notes?|details?|description|comments?|info)\s*$", re.I)

BUS_HEADER = re.compile(r"\b(metro\s*bus|buses|bus)\b", re.I)
RAIL_HEADER = re.compile(r"\b(metro\s*link|light\s*rail|rail|train)\b", re.I)
HOLIDAY_HEADER = re.compile(r"\b(holiday|observance|occasion)\b", re.I)
DATE_HEADER = re.compile(r"\b(date|observed|day)\b", re.I)
SERVICE_PHRASE = re.compile(
    r"\b(sunday|saturday|weekday|weekend|holiday|regular|normal|modified|no)\s+"
    r"(schedule|service)\b",
    re.I,
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_WORDS = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,\s*(\d{4}))?",
    re.I,
)
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")

URL_ATTRS = ("href", "src", "data-href", "data-url", "data-file")
URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)]+", re.I)

# Pick-id strategies, most explicit first. One URL contributes one id: the first
# pattern that matches wins, so a URL carrying ?pickId= is never also mined for
# a date-looking path segment.
PICK_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("pick_query", re.compile(r"[?&]pick(?:[_-]?id)?=([A-Za-z0-9._-]+)", re.I), False),
    ("pick_path", re.compile(r"/pick(?:[_-]?id)?[/=-]([A-Za-z0-9._-]+)", re.I), False),
    ("schedule_path",
     re.compile(r"/(?:schedules?|timetables?|pdfs?)/([A-Za-z0-9._-]+)/[^/]+\.pdf", re.I), True),
    ("dated_filename", re.compile(r"(20\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?[0-3]\d)", re.I), True),
)

NO_ALERTS = re.compile(
    r"\b(no\s+(?:current(?:ly)?|active|new)?\s*(?:rider\s+|service\s+|system\s+)?"
    r"(?:alerts?|advisories|advisory|detours?|disruptions?)|"
    r"there\s+are\s+no\s+alerts|all\s+routes\s+are\s+operating\s+normally)\b",
    re.I,
)
ALERT_HINT = re.compile(r"\b(alert|advisory|advisories|notice|detour|disruption|post)\b", re.I)
POSTED_LABEL = re.compile(r"\b(posted|updated|published|issued|effective)\b\s*[:–-]?\s*(.+)",
                          re.I)
CONTENT_HINT = re.compile(r"\b(main|content|entry|article|page-body|post-body)\b", re.I)


# ----------------------------------------------------------------- the tree --

@dataclass
class Node:
    """One element. `#document` is the synthetic root."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def walk(self) -> Iterator[Node]:
        """Pre-order, document order. Deterministic by construction (spec 2.8)."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find_all(self, *tags: str) -> list[Node]:
        wanted = set(tags)
        return [n for n in self.walk() if n.tag in wanted and n is not self]

    @property
    def signature(self) -> str:
        """class + id, for the hint matching every fallback strategy leans on."""
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}"

    @property
    def text(self) -> str:
        """Readable text of this subtree, whitespace collapsed.

        Deliberately NOT scrubbed of nonce-like tokens: extractors want the
        literal published string ("$2.50", "September 7, 2026"). Scrubbing is
        `normalize`'s job, and it exists for hashing, not for reading.
        """
        return _collapse("".join(_strings(self)))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        closers = _IMPLIED_CLOSE.get(tag)
        if closers:
            while len(self._stack) > 1 and self._stack[-1].tag in closers:
                self._stack.pop()
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> must not pop the stack. The inherited implementation fires
        # handle_endtag for a tag handle_starttag never pushed.
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return
        # A stray </div> is ignored rather than fatal. Unbalanced tags are
        # ordinary in CMS output and refusing to parse the page over one would
        # turn a cosmetic defect into an outage of the surveillance job.

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse(html: str) -> Node:
    """Parse `html` into a tree. Never raises on malformed input."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _strings(node: Node) -> Iterator[str]:
    for child in node.children:
        if isinstance(child, str):
            yield _WS.sub(" ", child)
        elif child.tag in DROP_TAGS:
            continue
        elif child.tag in INLINE_TAGS:
            yield from _strings(child)
        else:
            # Block boundary inside a cell: <td><p>A</p><p>B</p></td> is "A B",
            # not "AB". Inline children get no injected space, so <b>Metro</b>Link
            # stays "MetroLink" and a theme that wraps half a word in a <span>
            # does not read as a content change.
            yield " "
            yield from _strings(child)
            yield " "


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _lines(node: Node, drop: frozenset[str]) -> list[str]:
    """Text of `node` as one line per block element."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        line = _collapse("".join(buf))
        buf.clear()
        if line:
            out.append(line)

    def visit(current: Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                chunk = _WS.sub(" ", child)
                if chunk:
                    buf.append(chunk)
            elif child.tag in drop:
                continue
            elif child.tag in INLINE_TAGS:
                visit(child)
            else:
                flush()
                visit(child)
                flush()

    visit(node)
    flush()
    return out


def _scrub(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # also folds NBSP to a plain space
    text = _ZERO_WIDTH.sub("", text)
    for _, pattern, replacement in NOISE_PATTERNS:
        text = pattern.sub(replacement, text)
    return "\n".join(line for line in (_collapse(x) for x in text.split("\n")) if line)


# ------------------------------------------------------------- normalization --

def normalize(html: str) -> str:
    """Strip HTML to stable, comparable text.

    THIS IS THE LOAD-BEARING FUNCTION. Raw HTML changes on every single request:
    analytics IDs, CSRF nonces, cache-busting query strings, rotating hero
    images, session tokens, timestamps. Hashing raw HTML means every drift check
    is a false positive, and a check that always fires is a check nobody reads.

    script/style/noscript/svg/head go entirely, attributes go with them (only
    text survives), whitespace collapses, and anything shaped like a nonce or a
    build hash is replaced with a stable placeholder rather than deleted -- so a
    diff still shows that *something* opaque sits there, without showing a
    different value every hour.

    Output is one line per block element. That shape is what makes `web diff`
    legible; a single collapsed line would diff as "everything changed".
    """
    return _scrub("\n".join(_lines(parse(html), DROP_TAGS)))


def content_hash(text: str) -> str:
    """sha256 of normalized content. Stable across cosmetic republishing.

    Takes text, not HTML, so the caller cannot accidentally hash the raw bytes:
    the whole point (spec 6.7) is that the hash is over what the page *said*.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_region(root: Node) -> tuple[Node, str]:
    """The subtree that holds the page's own content, and how it was found."""
    main = [n for n in root.walk() if n.tag == "main"]
    if main:
        return main[0], "main_element"
    roled = [n for n in root.walk() if n.attrs.get("role", "").lower() == "main"]
    if roled:
        return roled[0], "role_main"
    hinted = [
        n for n in root.walk()
        if n.tag in ("div", "section") and CONTENT_HINT.search(n.signature)
    ]
    if hinted:
        # Longest wins; ties break on document order because max() keeps the
        # first maximum, which keeps two runs identical (spec 2.8).
        return max(hinted, key=lambda n: len(n.text)), "content_class"
    articles = [n for n in root.walk() if n.tag == "article"]
    if len(articles) == 1:
        return articles[0], "single_article"
    body = [n for n in root.walk() if n.tag == "body"]
    if body:
        return body[0], "body"
    return root, "whole_document"


def main_text(html: str) -> dict[str, Any]:
    """Normalized text of the page's main content region.

    Spec 6.7 is explicit about the order: extract main content, normalize, hash.
    Hashing the whole document instead would make a site-wide footer edit look
    like a fare change on the fares page -- true, useless, and the fastest way
    to train a reader to ignore the alert.
    """
    root = parse(html)
    region, strategy = _content_region(root)
    text = _scrub("\n".join(_lines(region, DROP_TAGS | CHROME_TAGS)))
    if not text:
        # The chrome filter over-reached, or the region really is empty. Retry
        # once keeping chrome, and say so, rather than reporting a blank page.
        text = _scrub("\n".join(_lines(region, DROP_TAGS)))
        strategy = f"{strategy}+chrome_kept"
    return {"text": text, "strategy": strategy, "region_tag": region.tag}


def robots_directives(html: str) -> dict[str, Any]:
    """`<meta name="robots">` directives, read from the page itself.

    The Rider Alerts page carries noarchive/nosnippet, and spec 9 says to treat
    such a page as capture-for-reference and never as a live dependency. Reading
    it out of the HTML rather than out of config means a page that *acquires*
    the directive later is noticed the next time it is captured.
    """
    root = parse(html)
    directives: list[str] = []
    for meta in root.walk():
        if meta.tag != "meta" or meta.attrs.get("name", "").lower() not in ("robots", "googlebot"):
            continue
        content = meta.attrs.get("content", "").strip()
        if content:
            directives.append(content)
    raw = ", ".join(directives)
    lowered = raw.lower()
    return {
        "raw": raw,
        "noarchive": "noarchive" in lowered,
        "nosnippet": "nosnippet" in lowered,
        "noindex": "noindex" in lowered,
    }


# ---------------------------------------------------------------- money --

def parse_money(text: str) -> dict[str, Any] | None:
    """Parse a printed price into integer cents.

    Integer cents is the house rule and it is load-bearing: binary floating
    point cannot represent 2.50, so a fare table round-tripped through float
    eventually publishes $2.4999999. Decimal parses the printed string exactly
    and the result is an int the Kotlin side can compare byte for byte.

    Returns None when the text carries no price at all -- that is a signal the
    caller uses to skip a row, not an error.
    """
    matches = list(MONEY.finditer(text))
    if not matches:
        if FREE_CELL.match(text):
            return {
                "price_usd": "0.00",
                "price_cents": 0,
                "prices_cents": [0],
                "is_range": False,
                "basis": "free_text",
                "raw_price": _collapse(text),
            }
        return None
    cents = [_to_cents(m) for m in matches]
    return {
        "price_usd": _dollars(cents[0]),
        "price_cents": cents[0],
        # A cell reading "$2.50 - $5.00" keeps both numbers. Silently taking the
        # first would bundle the cheap end of a range as the fare.
        "prices_cents": cents,
        "is_range": len(cents) > 1,
        "basis": "amount",
        "raw_price": _collapse(text),
    }


def _to_cents(match: re.Match[str]) -> int:
    whole = match.group(1).replace(",", "")
    frac = match.group(2) or "0"
    amount = Decimal(f"{whole}.{frac}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


# ----------------------------------------------------------- shared walkers --

def _with_headings(root: Node, tags: tuple[str, ...]) -> list[tuple[str, Node]]:
    """Nodes of the given tags, each paired with the heading above it.

    Nested matches are skipped: a table inside a table is one finding, not two,
    and the inner one would otherwise be attributed to a heading it never saw.
    """
    out: list[tuple[str, Node]] = []
    inside: set[int] = set()
    heading = ""
    for node in root.walk():
        if id(node) in inside:
            continue
        if node.tag in HEADING_TAGS:
            heading = node.text
        elif node.tag in tags:
            out.append((heading, node))
            inside.update(id(inner) for inner in node.walk())
    return out


def _cells(row: Node) -> list[Node]:
    direct = [c for c in row.children if isinstance(c, Node) and c.tag in ("td", "th")]
    return direct or [n for n in row.find_all("td", "th")]


def _grid(table: Node) -> list[list[tuple[bool, str]]]:
    """The table as rows of (is_header_cell, text). Empty rows dropped."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [(c.tag == "th", c.text) for c in _cells(tr)]
        if cells and any(text for _, text in cells):
            rows.append(cells)
    return rows


def _split_header(grid: list[list[tuple[bool, str]]]) -> tuple[list[str], list[list[str]]]:
    """Split a grid into (header texts, body rows).

    A header row is one made of <th>, or -- for the themes that use <td>
    throughout -- a first row carrying no price and no service word.
    """
    first = grid[0]
    is_header = all(flag for flag, _ in first) or not any(
        MONEY.search(text) or SERVICE_PHRASE.search(text) for _, text in first
    )
    if is_header and len(grid) > 1:
        return [text for _, text in first], [[t for _, t in row] for row in grid[1:]]
    return [], [[t for _, t in row] for row in grid]


def _leaf_blocks(root: Node) -> list[tuple[str, str]]:
    """(heading, text) for every block element that holds only inline content.

    The last-resort strategy for both fares and holidays: if the data stopped
    being a table it is probably a list or a run of paragraphs, and those still
    read as "label, separator, value".
    """
    out: list[tuple[str, str]] = []
    heading = ""
    for node in root.walk():
        if node.tag in HEADING_TAGS:
            heading = node.text
            continue
        if node.tag not in LEAF_BLOCK_TAGS:
            continue
        if any(isinstance(c, Node) and c.tag not in INLINE_TAGS for c in node.children):
            continue  # not a leaf; its children will be visited instead
        text = node.text
        if text:
            out.append((heading, text))
    return out


def _parse_date(text: str) -> str | None:
    """ISO date from printed text, or None.

    Returns None when the year is absent rather than assuming the current one.
    A holiday mapping that guesses the year is wrong for four months a year and
    silently right the rest of the time, which is the worst possible failure
    mode for a bundled table.
    """
    iso = DATE_ISO.search(text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    words = DATE_WORDS.search(text)
    if words and words.group(3):
        month = MONTHS[words.group(1).lower()[:3]]
        return f"{int(words.group(3)):04d}-{month:02d}-{int(words.group(2)):02d}"
    slash = DATE_SLASH.search(text)
    if slash:
        year = int(slash.group(3))
        year += 2000 if year < 100 else 0
        return f"{year:04d}-{int(slash.group(1)):02d}-{int(slash.group(2)):02d}"
    return None


def _bound(rows: list[Any], cap: int) -> tuple[list[Any], bool]:
    return rows[:cap], len(rows) > cap


# ----------------------------------------------------------------- dispatch --

def extract(html: str, extractor: str) -> dict[str, Any]:
    """Run one named extractor over `html`.

    Raises ExtractionFailed when the expected structure is absent. That means
    Metro redesigned the page and the extractor needs updating, which is exactly
    the signal the surveillance job exists to produce -- returning an empty list
    would hide it behind a result that looks successful.

    An unknown extractor name raises UsageError instead: that is a typo in
    sources.toml, not a page redesign, and the two need different remedies.
    """
    handler = _DISPATCH.get(extractor)
    if handler is None:
        raise UsageError(
            f"Unknown extractor {extractor!r}.",
            remedy="`extractor` in sources.toml must be one of: "
                   + ", ".join(EXTRACTORS)
                   + ". Fix the page entry, or add the new extractor to "
                     "core/web/extract.py and to EXTRACTORS.",
            extractor=extractor,
            known=list(EXTRACTORS),
        )
    return {"extractor": extractor, **handler(html)}


# --------------------------------------------------------------- fare_table --

def _fares_from_tables(root: Node) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for heading, table in _with_headings(root, ("table",)):
        grid = _grid(table)
        if not grid:
            continue
        if not any(MONEY.search(text) or FREE_CELL.match(text)
                   for row in grid for _, text in row):
            continue  # a layout table, not a fare table
        caption = next((c.text for c in table.find_all("caption") if c.text), "")
        header, body = _split_header(grid)
        price_col = _price_column(body)
        section = ""
        for cells in body:
            if len(cells) == 1:
                # A full-width row is a section label ("Reduced Fare", "Passes").
                if cells[0] and not MONEY.search(cells[0]):
                    section = cells[0]
                continue
            index = _price_index(cells, price_col)
            if index is None:
                continue
            money = parse_money(cells[index])
            if money is None:
                continue
            label_index = next((i for i in range(len(cells)) if i != index and cells[i]), None)
            if label_index is None:
                continue
            rows.append(
                _fare_row(
                    category=section or heading or caption,
                    fare_type=cells[label_index],
                    money=money,
                    notes=_notes(cells, header, {index, label_index}),
                    via="table_row",
                    section=section,
                    heading=heading or caption,
                )
            )
    return rows


def _price_column(body: list[list[str]]) -> int | None:
    """The column index carrying prices, chosen by majority.

    Per-column beats per-row: a notes column reading "free transfer with any
    $2.50 fare" would otherwise capture the price on the one row where the
    marketing copy mentions a number.
    """
    counts: dict[int, int] = {}
    for cells in body:
        for i, text in enumerate(cells):
            if MONEY.search(text):
                counts[i] = counts.get(i, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return min(i for i, n in counts.items() if n == best)


def _price_index(cells: list[str], price_col: int | None) -> int | None:
    if price_col is not None and price_col < len(cells):
        if MONEY.search(cells[price_col]) or FREE_CELL.match(cells[price_col]):
            return price_col
    return next(
        (i for i, text in enumerate(cells) if MONEY.search(text) or FREE_CELL.match(text)),
        None,
    )


def _notes(cells: list[str], header: list[str], used: set[int]) -> str:
    parts = []
    for i, text in enumerate(cells):
        if i in used or not text:
            continue
        label = header[i] if i < len(header) else ""
        if label and not GENERIC_NOTE_HEADERS.match(label):
            parts.append(f"{label}: {text}")
        else:
            parts.append(text)
    return " | ".join(parts)


def _fare_row(category: str, fare_type: str, money: dict[str, Any], notes: str,
              via: str, section: str = "", heading: str = "") -> dict[str, Any]:
    return {
        "category": category,
        "fare_type": fare_type,
        "price_usd": money["price_usd"],
        "price_cents": money["price_cents"],
        "prices_cents": money["prices_cents"],
        "is_range": money["is_range"],
        "price_basis": money["basis"],
        "raw_price": money["raw_price"],
        "notes": notes,
        "section": section,
        "heading": heading,
        "via": via,
    }


def _fares_from_dl(root: Node) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for heading, dl in _with_headings(root, ("dl",)):
        term = ""
        for child in dl.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "dt":
                term = child.text
            elif child.tag == "dd" and term:
                value = child.text
                money = parse_money(value)
                label, note = term, MONEY.sub("", value).strip(" -–—:;,|")
                if money is None:
                    # Some themes invert it: the price is the term.
                    money = parse_money(term)
                    if money is None:
                        continue
                    label, note = value, ""
                rows.append(
                    _fare_row(
                        category=heading,
                        fare_type=label,
                        money=money,
                        notes=note,
                        via="definition_list",
                        heading=heading,
                    )
                )
    return rows


def _fares_from_lines(root: Node) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for heading, line in _leaf_blocks(root):
        money = parse_money(line)
        if money is None:
            if not FREE_SUFFIX.search(line):
                continue
            money = {"price_usd": "0.00", "price_cents": 0, "prices_cents": [0],
                     "is_range": False, "basis": "free_text", "raw_price": line}
            label = FREE_SUFFIX.sub("", line).strip(" -–—:;,|")
        else:
            label = line[: MONEY.search(line).start()].strip(" -–—:;,|")
        # A label has to look like a label. Without this, every paragraph of
        # marketing copy that mentions a dollar amount becomes a fare row.
        if not (2 <= len(label) <= 80) or not re.search(r"[A-Za-z]", label):
            continue
        rows.append(
            _fare_row(
                category=heading,
                fare_type=label,
                money=money,
                notes="",
                via="labelled_line",
                heading=heading,
            )
        )
    return rows


def _fare_table(html: str) -> dict[str, Any]:
    root = parse(html)
    attempts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    strategy = ""
    for name, fn in (
        ("table", _fares_from_tables),
        ("definition_list", _fares_from_dl),
        ("labelled_lines", _fares_from_lines),
    ):
        found = fn(root)
        attempts.append({"strategy": name, "rows": len(found)})
        if found and not rows:
            rows, strategy = found, name

    if not rows:
        raise ExtractionFailed(
            "No fare rows found: no table, definition list, or labelled line on this "
            "page carried a price.",
            remedy="Open the fares page and find where the prices moved to, then add or "
                   "repair a strategy in core/web/extract.py (_fares_from_tables, "
                   "_fares_from_dl, _fares_from_lines). Until then `stl bundle fares` "
                   "has nothing current to publish and the app's bundled fare table is "
                   "unverified -- check the printed prices by hand before shipping.",
            extractor="fare_table",
            strategies_tried=[a["strategy"] for a in attempts],
            tables_seen=len(root.find_all("table")),
            dollar_signs=normalize(html).count("$"),
        )

    items, truncated = _bound(rows, MAX_ROWS)
    return {
        "strategy": strategy,
        "attempts": attempts,
        "rows": items,
        "row_count": len(rows),
        "truncated": truncated,
        "currency": "USD",
        "free_rows": sum(1 for r in rows if r["price_cents"] == 0),
        "ranged_rows": sum(1 for r in rows if r["is_range"]),
        "notes": [
            "price_cents is the authoritative field; price_usd is its decimal "
            "rendering. Fares are compared and bundled in integer cents because "
            "binary floating point cannot represent 2.50.",
            f"Matched by the {strategy!r} strategy. If a future reader finds this "
            "wrong, that is the function to fix.",
        ],
    }


# ------------------------------------------------------------- holiday_table --

def _mode_of(text: str) -> str | None:
    """Which mode a header or heading names. Bus and rail never merge.

    MetroBus maps to Sunday service on a holiday while MetroLink maps to
    Weekend -- different vocabularies for different systems. Copying one into
    the other publishes a schedule riders will miss trains over, so a cell whose
    mode cannot be determined is left as None rather than filled in.
    """
    rail = bool(RAIL_HEADER.search(text))
    bus = bool(BUS_HEADER.search(text))
    if rail and not bus:
        return "rail"
    if bus and not rail:
        return "bus"
    return None


def _holiday_row(holiday: str, date_text: str) -> dict[str, Any]:
    return {
        "holiday": holiday,
        "date": date_text or None,
        "date_iso": _parse_date(date_text or holiday),
        "bus_service": None,
        "rail_service": None,
        "other_services": {},
        "via": "",
    }


def _holidays_from_columns(root: Node) -> list[dict[str, Any]]:
    """One table, one column per mode."""
    rows: list[dict[str, Any]] = []
    for _, table in _with_headings(root, ("table",)):
        grid = _grid(table)
        if not grid:
            continue
        header, body = _split_header(grid)
        if not header:
            continue
        modes = {i: _mode_of(text) for i, text in enumerate(header)}
        if "bus" not in modes.values() and "rail" not in modes.values():
            continue
        date_col = next(
            (i for i, text in enumerate(header)
             if DATE_HEADER.search(text) and not HOLIDAY_HEADER.search(text)),
            None,
        )
        name_col = next(
            (i for i, text in enumerate(header) if HOLIDAY_HEADER.search(text)),
            next((i for i in range(len(header)) if modes.get(i) is None and i != date_col), 0),
        )
        for cells in body:
            if len(cells) < 2 or name_col >= len(cells) or not cells[name_col]:
                continue
            date_text = cells[date_col] if date_col is not None and date_col < len(cells) else ""
            row = _holiday_row(cells[name_col], date_text)
            row["via"] = "mode_columns"
            for i, value in enumerate(cells):
                if i in (name_col, date_col) or not value:
                    continue
                mode = modes.get(i)
                if mode == "bus":
                    row["bus_service"] = value
                elif mode == "rail":
                    row["rail_service"] = value
                elif i < len(header) and header[i]:
                    # A Call-A-Ride column is neither bus nor rail. Kept rather
                    # than dropped, because "we did not model this" is a finding.
                    row["other_services"][header[i]] = value
            if row["bus_service"] or row["rail_service"] or row["other_services"]:
                rows.append(row)
    return rows


def _holidays_from_mode_tables(root: Node) -> list[dict[str, Any]]:
    """One table per mode, keyed by the heading above it, merged by holiday."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for heading, table in _with_headings(root, ("table",)):
        mode = _mode_of(heading)
        if mode is None:
            continue
        grid = _grid(table)
        if not grid:
            continue
        header, body = _split_header(grid)
        date_col = next(
            (i for i, text in enumerate(header)
             if DATE_HEADER.search(text) and not HOLIDAY_HEADER.search(text)),
            None,
        )
        for cells in body:
            if len(cells) < 2 or not cells[0]:
                continue
            date_text = cells[date_col] if date_col is not None and date_col < len(cells) else ""
            service = next(
                (c for i, c in enumerate(cells) if i not in (0, date_col) and c), ""
            )
            if not service:
                continue
            key = (cells[0].casefold(), _parse_date(date_text or cells[0]) or "")
            if key not in merged:
                row = _holiday_row(cells[0], date_text)
                row["via"] = "mode_per_table"
                merged[key] = row
                order.append(key)
            merged[key][f"{mode}_service"] = service
    return [merged[k] for k in order]


def _holidays_from_lines(root: Node) -> list[dict[str, Any]]:
    """Prose: "Labor Day: MetroBus runs a Sunday schedule; MetroLink a Weekend one"."""
    rows: list[dict[str, Any]] = []
    for heading, line in _leaf_blocks(root):
        head, _, tail = line.partition(":")
        holiday = head if tail else heading
        body = tail or line
        if not holiday:
            continue
        row = _holiday_row(holiday.strip(), line)
        row["via"] = "prose_line"
        for clause in re.split(r"[;.|]", body):
            mode = _mode_of(clause)
            if mode is None:
                continue
            phrase = SERVICE_PHRASE.search(clause)
            value = phrase.group(0) if phrase else _collapse(
                BUS_HEADER.sub("", RAIL_HEADER.sub("", clause))
            )
            if value:
                row[f"{mode}_service"] = value
        if row["bus_service"] or row["rail_service"]:
            rows.append(row)
    return rows


def _holiday_table(html: str) -> dict[str, Any]:
    root = parse(html)
    attempts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    strategy = ""
    for name, fn in (
        ("mode_columns", _holidays_from_columns),
        ("mode_per_table", _holidays_from_mode_tables),
        ("prose_lines", _holidays_from_lines),
    ):
        found = fn(root)
        attempts.append({"strategy": name, "rows": len(found)})
        if found and not rows:
            rows, strategy = found, name

    if not rows:
        raise ExtractionFailed(
            "No holiday rows found: nothing on this page pairs a holiday with a "
            "MetroBus or MetroLink service level.",
            remedy="Open the holiday-schedules page and see how the table is now laid "
                   "out, then repair a strategy in core/web/extract.py "
                   "(_holidays_from_columns, _holidays_from_mode_tables, "
                   "_holidays_from_lines). Do NOT paper over this by reusing last "
                   "year's mapping: `stl bundle holidays` would then publish a "
                   "holiday schedule nobody verified.",
            extractor="holiday_table",
            strategies_tried=[a["strategy"] for a in attempts],
            tables_seen=len(root.find_all("table")),
        )

    with_bus = sum(1 for r in rows if r["bus_service"])
    with_rail = sum(1 for r in rows if r["rail_service"])
    warnings: list[str] = []
    if not with_rail:
        warnings.append(
            "No MetroLink service level was found for any holiday. Rail is reported as "
            "null and is NOT assumed to match bus: MetroBus runs a Sunday schedule on a "
            "holiday while MetroLink runs a Weekend schedule, and those are different "
            "concepts."
        )
    if not with_bus:
        warnings.append(
            "No MetroBus service level was found for any holiday. Bus is reported as "
            "null rather than copied from rail."
        )
    items, truncated = _bound(rows, MAX_ROWS)
    return {
        "strategy": strategy,
        "attempts": attempts,
        "rows": items,
        "row_count": len(rows),
        "truncated": truncated,
        "with_bus_service": with_bus,
        "with_rail_service": with_rail,
        "dated_rows": sum(1 for r in rows if r["date_iso"]),
        "warnings": warnings,
        "notes": [
            "bus_service and rail_service are kept distinct on purpose. MetroBus maps "
            "to Sunday service on a holiday, MetroLink maps to Weekend service; they "
            "are different vocabularies and merging them is a real bug.",
            "date_iso is null when the page printed no year. The year is never guessed.",
        ],
    }


# ------------------------------------------------------------------ pick_id --

def _urls(root: Node, html: str) -> list[str]:
    seen: dict[str, None] = {}
    for node in root.walk():
        for attr in URL_ATTRS:
            value = node.attrs.get(attr, "").strip()
            if value and not value.startswith(("#", "javascript:", "mailto:", "tel:")):
                seen.setdefault(value, None)
    for match in URL_IN_TEXT.finditer(html):
        seen.setdefault(match.group(0), None)
    return list(seen)


def _pick_id(html: str) -> dict[str, Any]:
    root = parse(html)
    urls = _urls(root, html)
    pdf_urls = [u for u in urls if ".pdf" in u.lower()]

    found: dict[str, dict[str, Any]] = {}
    for url in urls:
        is_pdf = ".pdf" in url.lower()
        for name, pattern, pdf_only in PICK_PATTERNS:
            if pdf_only and not is_pdf:
                continue
            match = pattern.search(url)
            if not match:
                continue
            value = match.group(1)
            entry = found.setdefault(value, {"pick_id": value, "strategy": name, "urls": []})
            entry["urls"].append(url)
            break  # one URL yields one id; the most explicit pattern wins

    if not found:
        raise ExtractionFailed(
            "No pick id found: no link on this page carries a recognizable "
            "{pickId} token.",
            remedy="Open the upcoming-schedule-changes page, copy one schedule-PDF link, "
                   "and add its shape to PICK_PATTERNS in core/web/extract.py. Until "
                   "then the next service change has to be spotted by hand -- watch "
                   "`stl gtfs coverage` for feed_end_date instead.",
            extractor="pick_id",
            urls_seen=len(urls),
            pdf_urls_seen=len(pdf_urls),
            strategies_tried=[name for name, _, _ in PICK_PATTERNS],
            url_sample=sorted(urls)[:MAX_URL_SAMPLE],
        )

    items = []
    for value in sorted(found):
        entry = found[value]
        urls_for_id = sorted(dict.fromkeys(entry["urls"]))
        shown, truncated = _bound(urls_for_id, MAX_URLS_PER_ID)
        items.append(
            {
                "pick_id": value,
                "strategy": entry["strategy"],
                "urls": shown,
                "url_count": len(urls_for_id),
                "urls_truncated": truncated,
            }
        )
    return {
        "strategy": items[0]["strategy"],
        "pick_ids": [i["pick_id"] for i in items],
        "count": len(items),
        "items": items,
        "urls_seen": len(urls),
        "pdf_urls_seen": len(pdf_urls),
        "notes": [
            "Sorted lexicographically so two runs over one capture agree byte for "
            "byte (spec 2.8), not by position on the page.",
            "More than one id means the page is advertising more than one pick; the "
            "later id is the one that has not landed in the GTFS feed yet.",
        ],
    }


# --------------------------------------------------------------- alert_list --

def _alert_from(container: Node) -> dict[str, Any] | None:
    heading = next((h for h in container.find_all(*HEADING_TAGS) if h.text), None)
    title = heading.text if heading else ""
    posted_node, posted_text = _posted(container)
    # Identity, not equality: Node is a dataclass, so `==` compares whole
    # subtrees and would treat two structurally identical paragraphs as one.
    skip = {id(container)}
    for owned in (heading, posted_node):
        if owned is not None:
            skip.update(id(n) for n in owned.walk())

    body_parts = []
    for node in container.walk():
        if id(node) in skip or node.tag not in ("p", "li", "div"):
            continue
        if any(isinstance(c, Node) and c.tag not in INLINE_TAGS for c in node.children):
            continue  # a wrapper; its own children carry the text
        text = node.text
        if not text or text == title:
            continue
        body_parts.append(text)
    body = " ".join(dict.fromkeys(body_parts))
    if not title:
        title, body = (body[:120], body) if body else ("", "")
    if not title and not body:
        return None
    trimmed = body[:MAX_ALERT_BODY_CHARS]
    return {
        "title": title,
        "body": trimmed,
        "body_truncated": len(body) > MAX_ALERT_BODY_CHARS,
        "posted": posted_text or None,
        "posted_iso": _parse_date(posted_text) if posted_text else None,
    }


def _posted(container: Node) -> tuple[Node | None, str]:
    for node in container.walk():
        if node.tag == "time":
            stamp = node.attrs.get("datetime", "") or node.text
            if stamp:
                return node, _collapse(stamp)
    for node in container.walk():
        if node.tag not in LEAF_BLOCK_TAGS:
            continue
        text = node.text
        label = POSTED_LABEL.match(text)
        if label:
            return node, _collapse(label.group(2))
    return None, ""


def _alerts_from_articles(root: Node) -> list[dict[str, Any]]:
    out = []
    for article in root.find_all("article"):
        alert = _alert_from(article)
        if alert:
            out.append({**alert, "via": "article"})
    return out


def _alerts_from_hinted(root: Node) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    inside: set[int] = set()
    for node in root.walk():
        if id(node) in inside or node.tag not in ("div", "section", "li"):
            continue
        if not ALERT_HINT.search(node.signature):
            continue
        alert = _alert_from(node)
        if alert and (alert["title"] or alert["body"]):
            out.append({**alert, "via": "class_hint"})
            inside.update(id(inner) for inner in node.walk())
    return out


def _alerts_from_headings(root: Node) -> list[dict[str, Any]]:
    """Last resort: each heading in the content region starts an alert."""
    region, _ = _content_region(root)
    blocks = [
        n for n in region.walk()
        if n.tag in HEADING_TAGS
        or (n.tag in ("p", "li")
            and not any(isinstance(c, Node) and c.tag not in INLINE_TAGS
                        for c in n.children))
    ]
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for node in blocks:
        text = node.text
        if not text:
            continue
        if node.tag in HEADING_TAGS:
            if current and current["body"]:
                out.append(current)
            current = {"title": text, "body": "", "body_truncated": False,
                       "posted": None, "posted_iso": None, "via": "heading_run"}
            continue
        if current is None:
            continue
        label = POSTED_LABEL.match(text)
        if label and not current["posted"]:
            current["posted"] = _collapse(label.group(2))
            current["posted_iso"] = _parse_date(current["posted"])
            continue
        current["body"] = f"{current['body']} {text}".strip()[:MAX_ALERT_BODY_CHARS]
    if current and current["body"]:
        out.append(current)
    return out


def _alert_list(html: str) -> dict[str, Any]:
    root = parse(html)
    attempts: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    strategy = ""
    for name, fn in (("article", _alerts_from_articles), ("class_hint", _alerts_from_hinted)):
        found = fn(root)
        attempts.append({"strategy": name, "alerts": len(found)})
        if found and not alerts:
            alerts, strategy = found, name

    body_text = main_text(html)["text"]
    if not alerts and NO_ALERTS.search(body_text):
        # "No active alerts" is a legitimate state and evidence, not silence.
        # Falling through to the heading-run strategy here would turn the page's
        # own "there are no alerts" notice into an alert titled "Rider Alerts".
        return {
            "strategy": "declared_empty",
            "attempts": attempts,
            "alerts": [],
            "count": 0,
            "truncated": False,
            "empty_reason": "The page states there are no active alerts.",
            "notes": [
                "An empty list here is the page's own claim, matched against its text. "
                "An empty list with no such statement raises ExtractionFailed instead.",
            ],
        }

    if not alerts:
        found = _alerts_from_headings(root)
        attempts.append({"strategy": "heading_run", "alerts": len(found)})
        if found:
            alerts, strategy = found, "heading_run"

    if not alerts:
        raise ExtractionFailed(
            "No alerts found, and the page does not say there are none.",
            remedy="Open the rider-alerts page. If it genuinely lists no alerts, add its "
                   "wording to NO_ALERTS in core/web/extract.py; if it lists alerts in a "
                   "new markup shape, add a strategy alongside _alerts_from_articles. "
                   "This page carries noarchive/nosnippet and is capture-for-reference "
                   "only (spec 9) -- nothing in the app should be blocking on it.",
            extractor="alert_list",
            strategies_tried=[a["strategy"] for a in attempts],
            articles_seen=len(root.find_all("article")),
            characters_of_text=len(body_text),
        )

    items, truncated = _bound(alerts, MAX_ALERTS)
    return {
        "strategy": strategy,
        "attempts": attempts,
        "alerts": items,
        "count": len(alerts),
        "truncated": truncated,
        "dated": sum(1 for a in alerts if a.get("posted_iso")),
        "notes": [
            "Capture-for-reference only. The Rider Alerts page carries "
            "noarchive/nosnippet and spec 9 forbids treating it as a live dependency; "
            "use the GTFS-RT alerts feed for anything the app renders.",
        ],
    }


# --------------------------------------------------------------------- text --

def _text(html: str) -> dict[str, Any]:
    root = parse(html)
    main = main_text(html)
    text = main["text"]

    heading, source = "", "none"
    region, _ = _content_region(root)
    for node in region.walk():
        if node.tag in HEADING_TAGS and node.text:
            heading, source = node.text, f"{node.tag} in {main['strategy']}"
            break
    if not heading:
        for node in root.walk():
            if node.tag in HEADING_TAGS and node.text:
                heading, source = node.text, f"{node.tag} outside the content region"
                break
    if not heading:
        title = next((n.text for n in root.find_all("title") if n.text), "")
        if title:
            heading, source = title, "document title"

    words = text.split()
    if not words:
        raise ExtractionFailed(
            "The page yielded no readable text after normalization.",
            remedy="Fetch it again and look at the stored HTML: this is what a "
                   "JavaScript-rendered shell, a cookie wall, or a redirect page looks "
                   "like from here. If Metro moved this page behind client-side "
                   "rendering, the capture has to change, not the extractor -- and "
                   "spec 7 does not budget for a headless browser.",
            extractor="text",
            region_strategy=main["strategy"],
            raw_characters=len(html),
        )

    body, truncated = (text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS)
    headings = [n.text for n in region.walk() if n.tag in HEADING_TAGS and n.text]
    shown_headings, headings_truncated = _bound(headings, 50)
    return {
        "strategy": main["strategy"],
        "region_tag": main["region_tag"],
        "text": body,
        "truncated": truncated,
        "word_count": len(words),
        "char_count": len(text),
        "line_count": text.count("\n") + 1,
        "first_heading": heading or None,
        "first_heading_source": source,
        "headings": shown_headings,
        "headings_truncated": headings_truncated,
    }


_DISPATCH = {
    "fare_table": _fare_table,
    "holiday_table": _holiday_table,
    "pick_id": _pick_id,
    "alert_list": _alert_list,
    "text": _text,
}
