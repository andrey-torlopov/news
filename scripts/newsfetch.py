#!/usr/bin/env python3
"""Download and compact RSS/Atom feeds and article pages as JSON Lines.

The tool is intentionally limited to transport and deterministic parsing. News
selection, scoring, deduplication and report generation remain in news/head.md.

Input formats:
  feeds: <category slug>\t<source name>\t<URL>\t<tier>
  pages: <candidate key>\t<URL>

Every output line is one JSON object with a ``kind`` field. Raw response bodies
are never written to stdout.
"""

from __future__ import annotations

import argparse
import email.utils
import html
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, TextIO
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET


VERSION = "1.0.0"
MSK = timezone(timedelta(hours=3))
USER_AGENT = (
    "Mozilla/5.0 (compatible; WeeklyNews/1.0; "
    "+https://github.com/andrey-torlopov/news)"
)
SOURCE_DOMAINS = {
    "doi.org",
    "arxiv.org",
    "github.com",
    "gitlab.com",
    "zenodo.org",
    "biorxiv.org",
    "medrxiv.org",
    "osf.io",
}
CHALLENGE_PATTERN = re.compile(
    br"just a moment|attention required|challenge-platform|captcha", re.I
)
COMPRESSION_ERROR_PATTERN = re.compile(
    r"content encoding|unrecognized.*encoding|decompress|brotli|compressed", re.I
)
NETWORK_RETURN_CODES = {5, 6, 7, 18, 28, 35, 52, 55, 56, 92}


@dataclass(frozen=True)
class FetchResult:
    http: int
    content_type: str
    effective_url: str
    body: bytes
    error: str
    returncode: int
    request_count: int = 1
    retry_mechanism: str | None = None
    retry_succeeded: bool | None = None

    @property
    def byte_count(self) -> int:
        return len(self.body)


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    unescaped = html.unescape(str(value))
    without_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", without_tags).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def text_of(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return clean_text(" ".join(element.itertext()))


def parse_datetime(value: object | None) -> tuple[date | datetime | None, bool | None]:
    normalized = clean_text(value)
    if not normalized:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        try:
            return date.fromisoformat(normalized), False
        except ValueError:
            return None, None

    parsed: datetime | None = None
    try:
        parsed = email.utils.parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        pass

    if parsed is None:
        iso_value = normalized.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_value)
        except ValueError:
            match = re.search(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
                r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
                iso_value,
            )
            if match:
                candidate = match.group(0).replace("Z", "+00:00")
                if re.search(r"[+-]\d{4}$", candidate):
                    candidate = candidate[:-5] + candidate[-5:-2] + ":" + candidate[-2:]
                try:
                    parsed = datetime.fromisoformat(candidate)
                except ValueError:
                    pass

    if parsed is None:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK)
    return parsed.astimezone(MSK), True


def iso_value(parsed: date | datetime | None, has_time: bool | None) -> str | None:
    if parsed is None:
        return None
    if has_time and isinstance(parsed, datetime):
        return parsed.isoformat(timespec="seconds")
    return parsed.isoformat()


def validate_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL должен использовать http или https и содержать домен")
    return value


def curl_fetch(
    url: str,
    *,
    curl_path: str,
    timeout: int,
    max_body_bytes: int,
    compressed: bool,
) -> FetchResult:
    with tempfile.TemporaryDirectory(prefix="weeklynews-fetch-") as temp_dir:
        body_path = Path(temp_dir) / "body.bin"
        command = [
            curl_path,
            "-sS",
            "-L",
            "--max-time",
            str(timeout),
            "--connect-timeout",
            str(min(timeout, 10)),
            "--max-filesize",
            str(max_body_bytes),
            "-A",
            USER_AGENT,
            "-o",
            str(body_path),
            "-w",
            "%{http_code}\t%{content_type}\t%{url_effective}",
        ]
        if compressed:
            command.append("--compressed")
        command.append(url)

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as error:
            return FetchResult(
                http=0,
                content_type="",
                effective_url=url,
                body=b"",
                error=f"process timeout after {error.timeout}s",
                returncode=124,
            )

        body = body_path.read_bytes() if body_path.exists() else b""
        writeout = completed.stdout.decode("utf-8", "replace").split("\t", 2)
        http_code = int(writeout[0]) if writeout and writeout[0].isdigit() else 0
        content_type = writeout[1] if len(writeout) > 1 else ""
        effective_url = writeout[2] if len(writeout) > 2 else url
        error = completed.stderr.decode("utf-8", "replace").strip()
        if len(body) > max_body_bytes:
            body = b""
            error = f"response exceeded {max_body_bytes} bytes"
            returncode = 63
        else:
            returncode = completed.returncode
        return FetchResult(
            http=http_code,
            content_type=content_type,
            effective_url=effective_url,
            body=body,
            error=error,
            returncode=returncode,
        )


class FetchClient:
    """Fetch URLs and spend at most one retry per failure mechanism."""

    def __init__(
        self,
        *,
        curl_path: str,
        timeout: int,
        max_body_bytes: int,
        fetcher: Callable[..., FetchResult] = curl_fetch,
    ) -> None:
        self.curl_path = curl_path
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self.fetcher = fetcher
        self.network_retry_used = False
        self.compression_retry_used = False

    def _request(self, url: str, *, compressed: bool) -> FetchResult:
        return self.fetcher(
            url,
            curl_path=self.curl_path,
            timeout=self.timeout,
            max_body_bytes=self.max_body_bytes,
            compressed=compressed,
        )

    @staticmethod
    def _is_compression_failure(result: FetchResult) -> bool:
        return result.returncode == 61 or bool(
            COMPRESSION_ERROR_PATTERN.search(result.error)
        )

    @staticmethod
    def _is_network_failure(result: FetchResult) -> bool:
        return (
            result.http == 0
            or result.http >= 500
            or result.returncode in NETWORK_RETURN_CODES
        )

    def fetch(self, url: str) -> FetchResult:
        first = self._request(url, compressed=True)
        mechanism: str | None = None
        second: FetchResult | None = None

        if self._is_compression_failure(first) and not self.compression_retry_used:
            self.compression_retry_used = True
            mechanism = "сжатие"
            second = self._request(url, compressed=False)
        elif self._is_network_failure(first) and not self.network_retry_used:
            self.network_retry_used = True
            mechanism = "сеть"
            second = self._request(url, compressed=True)

        if second is None:
            return first
        succeeded = second.returncode == 0 and second.http == 200
        return replace(
            second,
            request_count=2,
            retry_mechanism=mechanism,
            retry_succeeded=succeeded,
        )


def find_child(element: ET.Element, names: set[str]) -> ET.Element | None:
    normalized_names = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in normalized_names:
            return child
    return None


def find_date(
    element: ET.Element,
) -> tuple[date | datetime | None, bool | None, str | None]:
    for wanted in ("pubdate", "updated", "published", "date"):
        for child in element.iter():
            if local_name(child.tag) == wanted:
                parsed, has_time = parse_datetime(text_of(child))
                if parsed is not None:
                    return parsed, has_time, wanted
    return None, None, None


def find_link(element: ET.Element) -> str:
    guid = ""
    for child in list(element):
        name = local_name(child.tag)
        if name == "link":
            href = child.attrib.get("href", "").strip()
            rel = child.attrib.get("rel", "alternate")
            if href and rel in {"alternate", ""}:
                return href
            value = text_of(child)
            if value.startswith(("http://", "https://")):
                return value
        if name == "guid":
            value = text_of(child)
            if value.startswith(("http://", "https://")):
                guid = value
    return guid


def feed_format(root: ET.Element) -> str:
    name = local_name(root.tag)
    if name == "rss":
        return "rss2"
    if name == "feed":
        return "atom"
    if name == "rdf":
        return "rdf"
    return name or "unknown"


def parse_feed(
    body: bytes,
    run_date: date,
    period: int,
    desc_limit: int,
) -> tuple[str, int, list[dict[str, object]], str | None, bool]:
    root = ET.fromstring(body)
    fmt = feed_format(root)
    if fmt not in {"rss2", "atom", "rdf"}:
        raise ValueError(f"неподдерживаемый корневой элемент: {root.tag}")
    entries = [
        element
        for element in root.iter()
        if local_name(element.tag) in {"item", "entry"}
    ]
    core_start = run_date - timedelta(days=period)
    field_start = run_date - timedelta(days=period + 1)
    parsed_entries: list[dict[str, object]] = []
    all_dates: list[tuple[date, date | datetime, bool | None]] = []

    for entry in entries:
        title = text_of(find_child(entry, {"title"}))
        link = find_link(entry)
        parsed_date, has_time, date_source = find_date(entry)
        if parsed_date is None:
            continue
        item_date = parsed_date.date() if isinstance(parsed_date, datetime) else parsed_date
        all_dates.append((item_date, parsed_date, has_time))
        if item_date < field_start or item_date >= run_date:
            continue
        description_element = find_child(
            entry, {"description", "summary", "content", "encoded"}
        )
        description = text_of(description_element)[:desc_limit]
        parsed_entries.append(
            {
                "title": title,
                "link": link,
                "date": iso_value(parsed_date, has_time),
                "has_time": bool(has_time),
                "date_source": date_source,
                "zone": "ядро" if item_date >= core_start else "поле",
                "description": description,
            }
        )

    oldest = min(all_dates, key=lambda item: item[0]) if all_dates else None
    oldest_iso = iso_value(oldest[1], oldest[2]) if oldest else None
    incomplete = bool(oldest and oldest[0] > field_start)
    return fmt, len(entries), parsed_entries, oldest_iso, incomplete


class PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.meta: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.canonical = ""
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.jsonld_parts: list[str] = []
        self.in_title = False
        self.in_script_jsonld = False
        self.hidden_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "script" and "ld+json" in attributes.get("type", "").lower():
            self.in_script_jsonld = True
        if tag == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
            )
            content = attributes.get("content", "")
            if key and content:
                self.meta.append((key.lower(), content))
        if tag == "link":
            href = attributes.get("href", "")
            rel = attributes.get("rel", "").lower()
            if href and "canonical" in rel:
                self.canonical = urljoin(self.base_url, href)
        if tag == "a" and attributes.get("href"):
            self.links.append(urljoin(self.base_url, attributes["href"]))
        if tag == "time" and attributes.get("datetime"):
            self.meta.append(("time:datetime", attributes["datetime"]))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_script_jsonld:
            self.in_script_jsonld = False
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_script_jsonld:
            self.jsonld_parts.append(data)
        if self.hidden_depth == 0:
            value = clean_text(data)
            if value:
                self.text_parts.append(value)


def jsonld_dates(value: object) -> list[str]:
    dates: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() == "datepublished" and isinstance(child, str):
                dates.append(child)
            dates.extend(jsonld_dates(child))
    elif isinstance(value, list):
        for child in value:
            dates.extend(jsonld_dates(child))
    return dates


def parse_page(body: bytes, effective_url: str, text_limit: int) -> dict[str, object]:
    encoding = "utf-8"
    match = re.search(br"charset=[\"']?([A-Za-z0-9._-]+)", body[:4096], re.I)
    if match:
        encoding = match.group(1).decode("ascii", "ignore") or "utf-8"
    try:
        decoded = body.decode(encoding, "replace")
    except LookupError:
        decoded = body.decode("utf-8", "replace")

    parser = PageParser(effective_url)
    parser.feed(decoded)
    date_candidates: list[tuple[int, str, date | datetime, bool | None]] = []
    meta_priority = [
        ("article:published_time", 2),
        ("datepublished", 3),
        ("prism.publicationdate", 4),
        ("dc.date", 4),
        ("time:datetime", 5),
    ]
    for key, content in parser.meta:
        normalized = key.replace("_", "").replace("-", "")
        for wanted, priority in meta_priority:
            wanted_normalized = wanted.replace("_", "").replace("-", "")
            if normalized == wanted_normalized:
                parsed, has_time = parse_datetime(content)
                if parsed is not None:
                    date_candidates.append((priority, wanted, parsed, has_time))
    for block in parser.jsonld_parts:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for value in jsonld_dates(data):
            parsed, has_time = parse_datetime(value)
            if parsed is not None:
                date_candidates.append((3, "jsonld:datePublished", parsed, has_time))

    date_candidates.sort(key=lambda item: item[0])
    published = date_candidates[0] if date_candidates else None
    source_links: list[str] = []
    for link in parser.links:
        host = urlparse(link).netloc.lower().split(":", 1)[0]
        host = host[4:] if host.startswith("www.") else host
        if any(host == domain or host.endswith("." + domain) for domain in SOURCE_DOMAINS):
            source_links.append(link)
    source_links = list(dict.fromkeys(source_links))

    return {
        "title": clean_text(" ".join(parser.title_parts)),
        "canonical": parser.canonical or effective_url,
        "page_date": iso_value(published[2], published[3]) if published else None,
        "page_date_has_time": bool(published[3]) if published else None,
        "page_date_source": published[1] if published else None,
        "source_links": source_links,
        "text": clean_text(" ".join(parser.text_parts))[:text_limit],
    }


def emit(record: dict[str, object], *, stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), file=stream)


def public_fetch_fields(result: FetchResult) -> dict[str, object]:
    return {
        "http": result.http,
        "content_type": result.content_type,
        "bytes": result.byte_count,
        "effective_url": result.effective_url,
        "error": result.error,
        "request_count": result.request_count,
        "retry_mechanism": result.retry_mechanism,
        "retry_succeeded": result.retry_succeeded,
    }


def data_lines(stream: Iterable[str]) -> Iterable[tuple[int, str]]:
    for line_number, raw_line in enumerate(stream, start=1):
        line = raw_line.rstrip("\r\n")
        if not line or line.lstrip().startswith("#"):
            continue
        yield line_number, line


def parse_feed_input(line: str) -> tuple[str, str, str, int]:
    fields = line.split("\t")
    if len(fields) != 4:
        raise ValueError("ожидаются 4 TSV-поля: slug, name, URL, tier")
    slug, name, url, raw_tier = (field.strip() for field in fields)
    if not slug or not name:
        raise ValueError("slug и name не могут быть пустыми")
    validate_http_url(url)
    try:
        tier = int(raw_tier)
    except ValueError as error:
        raise ValueError("tier должен быть целым числом") from error
    if tier not in {1, 2}:
        raise ValueError("tier должен быть равен 1 или 2")
    return slug, name, url, tier


def parse_page_input(line: str) -> tuple[str, str]:
    fields = line.split("\t")
    if len(fields) != 2:
        raise ValueError("ожидаются 2 TSV-поля: key и URL")
    key, url = (field.strip() for field in fields)
    if not key:
        raise ValueError("key не может быть пустым")
    validate_http_url(url)
    return key, url


def is_challenge(result: FetchResult) -> bool:
    return result.http in {403, 429} or bool(
        CHALLENGE_PATTERN.search(result.body[:100_000])
    )


def feeds_mode(args: argparse.Namespace, client: FetchClient) -> int:
    run_date = date.fromisoformat(args.date)
    had_input_errors = False
    for line_number, line in data_lines(sys.stdin):
        try:
            slug, name, url, tier = parse_feed_input(line)
        except ValueError as error:
            had_input_errors = True
            emit(
                {
                    "kind": "ERROR",
                    "mode": "feeds",
                    "line": line_number,
                    "error": str(error),
                }
            )
            continue

        fetched = client.fetch(url)
        base: dict[str, object] = {
            "kind": "DIAG",
            "slug": slug,
            "name": name,
            "url": url,
            "tier": tier,
            **public_fetch_fields(fetched),
        }
        if is_challenge(fetched):
            emit(
                {
                    **base,
                    "status": "БЛОК",
                    "format": None,
                    "items_total": 0,
                    "items_window": 0,
                    "oldest": None,
                    "incomplete": None,
                }
            )
            continue
        if fetched.http != 200 or fetched.returncode != 0:
            emit(
                {
                    **base,
                    "status": "СЛОМАН",
                    "format": None,
                    "items_total": 0,
                    "items_window": 0,
                    "oldest": None,
                    "incomplete": None,
                }
            )
            continue
        try:
            fmt, total, items, oldest, incomplete = parse_feed(
                fetched.body, run_date, args.period, args.desc
            )
        except (ET.ParseError, UnicodeError, ValueError) as error:
            emit(
                {
                    **base,
                    "status": "НЕЧИТАЕМ",
                    "format": None,
                    "items_total": 0,
                    "items_window": 0,
                    "oldest": None,
                    "incomplete": None,
                    "parse_error": str(error),
                }
            )
            continue
        emit(
            {
                **base,
                "status": "OK" if items else "ПУСТО",
                "format": fmt,
                "items_total": total,
                "items_window": len(items),
                "oldest": oldest,
                "incomplete": incomplete,
            }
        )
        for item in items:
            emit({"kind": "ITEM", "slug": slug, "name": name, "tier": tier, **item})
    return 2 if had_input_errors else 0


def empty_page_fields() -> dict[str, object]:
    return {
        "title": "",
        "canonical": "",
        "page_date": None,
        "page_date_has_time": None,
        "page_date_source": None,
        "source_links": [],
        "text": "",
    }


def pages_mode(args: argparse.Namespace, client: FetchClient) -> int:
    had_input_errors = False
    for line_number, line in data_lines(sys.stdin):
        try:
            key, url = parse_page_input(line)
        except ValueError as error:
            had_input_errors = True
            emit(
                {
                    "kind": "ERROR",
                    "mode": "pages",
                    "line": line_number,
                    "error": str(error),
                }
            )
            continue

        fetched = client.fetch(url)
        record: dict[str, object] = {
            "kind": "PAGE",
            "key": key,
            "url": url,
            **public_fetch_fields(fetched),
        }
        if is_challenge(fetched):
            emit({**record, "status": "БЛОК", **empty_page_fields()})
            continue
        if fetched.http != 200 or fetched.returncode != 0:
            emit({**record, "status": "СЛОМАН", **empty_page_fields()})
            continue
        try:
            parsed = parse_page(fetched.body, fetched.effective_url, args.text)
            emit({**record, "status": "OK", **parsed})
        except (UnicodeError, ValueError) as error:
            emit(
                {
                    **record,
                    "status": "НЕЧИТАЕМ",
                    "parse_error": str(error),
                    **empty_page_fields(),
                }
            )
    return 2 if had_input_errors else 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Загрузка лент и страниц для news/head.md с выводом JSONL"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--curl",
        dest="curl_path",
        help="путь к curl; по умолчанию ищется в PATH",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    def add_transport_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--timeout", default=35, type=positive_int)
        subparser.add_argument(
            "--max-body-bytes",
            default=10_000_000,
            type=positive_int,
            help="максимальный размер одного ответа в байтах",
        )

    feeds = subparsers.add_parser("feeds", help="фаза A: ленты в DIAG и ITEM")
    feeds.add_argument("--date", required=True, help="дата запуска YYYY-MM-DD")
    feeds.add_argument("--period", required=True, type=positive_int)
    feeds.add_argument("--desc", default=300, type=positive_int)
    add_transport_options(feeds)

    pages = subparsers.add_parser("pages", help="фаза D: страницы в PAGE")
    pages.add_argument("--text", default=1500, type=positive_int)
    add_transport_options(pages)
    return parser


def resolve_curl(explicit_path: str | None) -> str:
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"curl не найден или не исполняется: {explicit_path}")
        return str(path)
    discovered = shutil.which("curl")
    if not discovered:
        raise ValueError("curl не найден в PATH")
    return discovered


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        curl_path = resolve_curl(args.curl_path)
    except ValueError as error:
        parser.error(str(error))
    client = FetchClient(
        curl_path=curl_path,
        timeout=args.timeout,
        max_body_bytes=args.max_body_bytes,
    )
    if args.mode == "feeds":
        return feeds_mode(args, client)
    return pages_mode(args, client)


if __name__ == "__main__":
    raise SystemExit(main())
