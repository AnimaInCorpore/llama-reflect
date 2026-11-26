"""Fetch a public web page for a mentor and keep a text copy in .refl/sources/.

A mentor brings knowledge from outside the workspace, such as a manual or an API reference. Each page it uses is
saved as text with its URL and date, so the verifier confirms a quote by reading a file, and later reviewers can
find the page again without fetching it. Only Python's standard library is used.
"""
import hashlib
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SOURCES = ".refl/sources"
# Bytes read from a page, seconds per request, and characters of the saved text shown in the tool result. The
# mentor reads the rest of the saved copy with read_file.
MAX_BYTES = 2_000_000
TIMEOUT = 20
EXCERPT = 6000
# A generic client name, so a request says nothing about the user or the project.
_AGENT = "refl-mentor/1"
_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-", "application/json", "application/xml", "text/xml",
               "text/csv")
_SKIPPED = {"script", "style", "noscript", "svg", "template", "head", "iframe", "canvas"}
_BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "ul", "ol", "table",
           "tr", "blockquote", "dl", "dt", "dd", "figure", "figcaption", "hr", "form"}

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": ("Fetch a public web page, such as official documentation, a specification, or a manual, and "
                        "save its text under .refl/sources/. The result names the saved file and shows its first "
                        "lines with numbers. Read further with read_file on that file."),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def check_url(url):
    """Refuse anything but a public http(s) address, so a page cannot reach the local machine or network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Expected an http or https URL")
    if parts.username or parts.password:
        raise ValueError("A URL with credentials is not fetched")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or None)}
    except (socket.gaierror, UnicodeError) as error:
        raise ValueError(f"Cannot resolve {parts.hostname}: {error}") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{parts.hostname} is not a public address")
    return parts


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Text(HTMLParser):
    """Turn HTML into plain lines: headings marked with #, list items with -, code blocks kept as they are."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.line, self.skip, self.pre, self.title, self.in_title = [], [], 0, 0, "", False

    def _break(self):
        text = "".join(self.line)
        text = text.rstrip() if self.pre else " ".join(text.split())
        if text:
            self.lines.append(text)
        self.line = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in _SKIPPED:
            self.skip += 1
        elif tag == "pre":
            self._break()
            self.pre += 1
        elif tag == "br":
            self._break()
        elif re.fullmatch(r"h[1-6]", tag):
            self._break()
            self.line.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._break()
            self.line.append("- ")
        elif tag in _BLOCKS or tag in ("td", "th"):
            if tag in ("td", "th"):
                self.line.append(" | ")
            else:
                self._break()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in _SKIPPED:
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self._break()
            self.pre = max(0, self.pre - 1)
        elif re.fullmatch(r"h[1-6]", tag) or tag in _BLOCKS or tag == "li":
            self._break()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip:
            return
        if self.pre:
            parts = data.split("\n")
            for index, part in enumerate(parts):
                if index:
                    self._break()
                self.line.append(part)
        else:
            self.line.append(data)

    def text(self):
        self._break()
        return " ".join(self.title.split()), "\n".join(self.lines)


def html_to_text(markup):
    """The title and the readable text of an HTML page."""
    parser = _Text()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _charset(content_type, data):
    match = re.search(r"charset=([\w.-]+)", content_type or "", re.IGNORECASE) or \
        re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", data[:4096], re.IGNORECASE)
    if match:
        name = match.group(1)
        return name.decode("ascii", "replace") if isinstance(name, bytes) else name
    return "utf-8"


def source_path(url):
    """Where the text of `url` is saved, relative to the workspace."""
    parts = urllib.parse.urlsplit(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(parts.path).stem or "index").strip("-")[:60] or "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    host = re.sub(r"[^A-Za-z0-9.-]+", "-", parts.hostname or "host")
    return f"{SOURCES}/{host}/{stem}-{digest}.md"


def save_page(root, url, title, text, now=None):
    """Save a page's text with its URL and date. Return the relative path and the saved lines."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%MZ")
    lines = [f"Source: {url}", f"Fetched: {stamp}", f"Title: {title or '(none)'}", "", *text.splitlines()]
    relative = source_path(url)
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    ignore = Path(root) / SOURCES / ".gitignore"
    if not ignore.exists():
        # The pages belong to their authors, so Git leaves them out unless the user decides otherwise.
        ignore.write_text("*\n", encoding="utf-8")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return relative, lines


def result_for(relative, lines):
    """The tool result: the saved file and its first lines with numbers, as read_file shows them."""
    shown, size = [], 0
    for number, line in enumerate(lines, 1):
        entry = f"{number}: {line}"
        if size + len(entry) > EXCERPT:
            break
        shown.append(entry)
        size += len(entry) + 1
    more = (f"\n[Lines {len(shown) + 1} to {len(lines)} are in the saved file. Read them with read_file.]"
            if len(shown) < len(lines) else "")
    return {"output": f"Saved {relative} ({len(lines)} lines). Cite it as {relative}:<line>.\n"
                      + "\n".join(shown) + more}


def fetch_page(root, url):
    """Fetch a public page, save its text in the workspace, and return a tool result."""
    try:
        check_url(url)
        opener = urllib.request.build_opener(_Redirects)
        request = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "text/html, text/plain"})
        with opener.open(request, timeout=TIMEOUT) as response:
            final = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_BYTES + 1)
    except (ValueError, urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(reason) and os.name == "nt":
            # Windows adds a missing root certificate when one of its own clients needs it. Python only reads the
            # store, so a site that a browser or PowerShell opens can fail here until such a client opened it once.
            hint = (" Windows may lack the site's root certificate until a browser or PowerShell has opened the "
                    "site once.")
        return {"error": f"Cannot fetch {url}: {reason}.{hint}"}
    kind = content_type.split(";")[0].strip().lower()
    if kind not in ("text/html", "application/xhtml+xml") and not kind.startswith(_TEXT_TYPES):
        return {"error": f"{url} is {kind or 'of an unknown type'}, not a text page"}
    markup = data[:MAX_BYTES].decode(_charset(content_type, data), errors="replace")
    title, text = html_to_text(markup) if "html" in kind else ("", markup)
    if not text.strip():
        return {"error": f"{url} has no readable text"}
    relative, lines = save_page(root, final, title, text)
    result = result_for(relative, lines)
    if len(data) > MAX_BYTES:
        result["output"] += f"\n[The page was cut at {MAX_BYTES:,} bytes.]"
    return result


def saved_sources(root, limit=30):
    """The pages saved earlier, as "path (URL)" lines, newest first, for the Bookworm."""
    folder = Path(root) / SOURCES
    if not folder.is_dir():
        return []
    pages = sorted(folder.rglob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    lines = []
    for page in pages[:limit]:
        try:
            with page.open(encoding="utf-8", errors="replace") as source:
                first = source.readline().strip()
        except OSError:
            continue
        url = first.removeprefix("Source: ") if first.startswith("Source: ") else "unknown source"
        lines.append(f"{page.relative_to(root).as_posix()} ({url})")
    return lines
