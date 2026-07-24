#!/usr/bin/env python3
"""Mirror NJU's raw, school-wide course-query responses into this repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

APP_ENTRY_URL = "https://ehallapp.nju.edu.cn/jwapp/sys/kcbcx/*default/index.do"
COURSE_API_URL = "https://ehallapp.nju.edu.cn/jwapp/sys/kcbcx/modules/qxkcb/qxfbkccx.do"
PAGE_SIZE = 500
DEFAULT_START_YEAR = 2000
DEFAULT_TERMS = (1, 2)
SEMESTER_PATTERN = re.compile(r"^(\d{4})-(\d{4})-(\d+)$")


class ScrapeError(RuntimeError):
    """Base class for scraper failures safe to print in CI logs."""


class AuthenticationError(ScrapeError):
    """The CASTGC or derived eHall session is no longer valid."""


class ResponseFormatError(ScrapeError):
    """The upstream response did not match the expected course API shape."""


@dataclass(frozen=True)
class PageData:
    raw: bytes
    rows: list[Any]


@dataclass(frozen=True)
class SemesterResult:
    semester: str
    pages: int
    rows: int
    changed: bool


def extract_castgc(secret: str) -> str:
    """Accept either a bare CASTGC value or a full Cookie header fragment."""
    value = secret.strip()
    if not value:
        raise ScrapeError("NJU_CASTGC is empty")

    for part in value.split(";"):
        key, separator, candidate = part.strip().partition("=")
        if separator and key.strip().upper() == "CASTGC":
            candidate = candidate.strip()
            if candidate:
                return candidate
            break

    if "=" not in value:
        return value

    raise ScrapeError("NJU_CASTGC does not contain a usable CASTGC value")


def semester_display(semester: str) -> str:
    match = SEMESTER_PATTERN.fullmatch(semester)
    if not match:
        raise ValueError(f"Invalid semester code: {semester}")
    start, end, term = match.groups()
    return f"{start}-{end}学年 第{term}学期"


def semester_sort_key(semester: str) -> tuple[int, int, int]:
    match = SEMESTER_PATTERN.fullmatch(semester)
    if not match:
        raise ValueError(f"Invalid semester code: {semester}")
    return tuple(int(item) for item in match.groups())


def current_academic_start_year(today: date | None = None) -> int:
    current = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return current.year if current.month >= 7 else current.year - 1


def candidate_semesters(
    start_year: int,
    end_year: int,
    terms: Sequence[int] = DEFAULT_TERMS,
) -> list[str]:
    if start_year > end_year:
        raise ValueError("start_year must not be greater than end_year")
    if not terms or any(term < 1 for term in terms):
        raise ValueError("terms must contain positive integers")

    return [
        f"{year}-{year + 1}-{term}"
        for year in range(start_year, end_year + 1)
        for term in sorted(set(terms))
    ]


def make_query_setting(semester: str) -> str:
    setting = [
        {
            "name": "XNXQDM",
            "caption": "学年学期",
            "linkOpt": "AND",
            "builderList": "cbl_m_List",
            "builder": "m_value_equal",
            "value": semester,
            "value_display": semester_display(semester),
        },
        [
            [
                {
                    "name": "RWZTDM",
                    "value": "1",
                    "linkOpt": "and",
                    "builder": "equal",
                },
                {
                    "name": "RWZTDM",
                    "linkOpt": "or",
                    "builder": "isNull",
                },
            ]
        ],
        {
            "name": "CXYH",
            "value": True,
            "linkOpt": "AND",
            "builder": "equal",
        },
        {
            "name": "*order",
            "value": "+KKDWDM,+KCH,+KXH",
            "linkOpt": "AND",
            "builder": "m_value_equal",
        },
    ]
    return json.dumps(setting, ensure_ascii=False, separators=(",", ":"))


def parse_course_response(
    response: requests.Response, semester: str, page: int
) -> PageData:
    final_url = str(getattr(response, "url", ""))
    parsed_url = urlparse(final_url)
    if (
        parsed_url.hostname == "authserver.nju.edu.cn"
        or "/authserver/login" in final_url
    ):
        raise AuthenticationError("NJU authentication redirected to the login page")

    if response.status_code in (401, 403):
        raise AuthenticationError(
            f"Course endpoint rejected the session with HTTP {response.status_code}"
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ScrapeError(
            f"Course request failed with HTTP {response.status_code}"
        ) from exc

    raw = bytes(response.content)
    stripped = raw.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html")):
        raise AuthenticationError("Course endpoint returned HTML instead of JSON")

    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError) as exc:
        raise ResponseFormatError(
            f"Semester {semester} page {page} is not valid JSON"
        ) from exc

    try:
        rows = payload["datas"]["qxfbkccx"]["rows"]
    except (KeyError, TypeError) as exc:
        raise ResponseFormatError(
            f"Semester {semester} page {page} lacks datas.qxfbkccx.rows"
        ) from exc

    if not isinstance(rows, list):
        raise ResponseFormatError(
            f"Semester {semester} page {page} has a non-list rows value"
        )

    returned_semesters = {
        str(row["XNXQDM"]).strip()
        for row in rows
        if isinstance(row, dict) and row.get("XNXQDM") is not None
    }
    if returned_semesters and returned_semesters != {semester}:
        values = ", ".join(sorted(returned_semesters))
        raise ResponseFormatError(
            f"Semester {semester} page {page} returned rows for {values}"
        )

    return PageData(raw=raw, rows=rows)


class NJUCourseClient:
    def __init__(
        self,
        castgc: str,
        *,
        delay_seconds: float = 1.5,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        session: requests.Session | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self._last_request_at: float | None = None

        self.session.headers.update(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": APP_ENTRY_URL,
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.session.cookies.set(
            "CASTGC",
            castgc,
            domain="authserver.nju.edu.cn",
            path="/authserver",
            secure=True,
        )

    def authenticate(self) -> None:
        try:
            response = self.session.get(
                APP_ENTRY_URL,
                allow_redirects=True,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError("Failed to open the NJU course application") from exc

        final_url = str(response.url)
        parsed = urlparse(final_url)
        if (
            parsed.hostname == "authserver.nju.edu.cn"
            or "/authserver/login" in final_url
        ):
            raise AuthenticationError("NJU_CASTGC is invalid or expired")

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def fetch_page(self, semester: str, page: int) -> PageData:
        body = {
            "CXYH": "true",
            "querySetting": make_query_setting(semester),
            "*order": "+KKDWDM,+KCH,+KXH",
            "pageSize": str(PAGE_SIZE),
            "pageNumber": str(page),
        }

        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_rate_limit()
            try:
                response = self.session.post(
                    COURSE_API_URL,
                    data=body,
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
                self._last_request_at = time.monotonic()

                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"retryable HTTP {response.status_code}", response=response
                    )

                return parse_course_response(response, semester, page)
            except AuthenticationError:
                raise
            except ResponseFormatError:
                raise
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise ScrapeError(
                        f"Semester {semester} page {page} failed after "
                        f"{self.max_attempts} attempts"
                    ) from exc
                time.sleep(min(30.0, 2 ** (attempt - 1)))

        raise AssertionError("unreachable")


def directories_equal(left: Path, right: Path) -> bool:
    if not left.is_dir() or not right.is_dir():
        return False

    left_files = sorted(
        path.relative_to(left) for path in left.rglob("*") if path.is_file()
    )
    right_files = sorted(
        path.relative_to(right) for path in right.rglob("*") if path.is_file()
    )
    if left_files != right_files:
        return False

    return all(
        (left / relative).read_bytes() == (right / relative).read_bytes()
        for relative in left_files
    )


def publish_staging_directory(staging: Path, destination: Path) -> bool:
    """Replace destination only after a complete semester has been staged."""
    if directories_equal(staging, destination):
        shutil.rmtree(staging)
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"

    if destination.exists():
        destination.rename(backup)

    try:
        staging.rename(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)

    return True


def scrape_semester(
    client: NJUCourseClient,
    data_dir: Path,
    semester: str,
    first_page: PageData | None = None,
) -> SemesterResult:
    staging_root = data_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f"{semester}-{uuid.uuid4().hex}"
    staging.mkdir()

    page_number = 1
    page_count = 0
    row_count = 0

    try:
        while True:
            page_data = (
                first_page
                if page_number == 1 and first_page is not None
                else client.fetch_page(semester, page_number)
            )

            if not page_data.rows:
                if page_number == 1:
                    raise ScrapeError(f"Semester {semester} returned no course rows")
                break

            (staging / f"page_{page_number:03d}.json").write_bytes(page_data.raw)
            page_count += 1
            row_count += len(page_data.rows)

            if len(page_data.rows) < PAGE_SIZE:
                break
            page_number += 1

        changed = publish_staging_directory(staging, data_dir / semester)
        return SemesterResult(
            semester=semester,
            pages=page_count,
            rows=row_count,
            changed=changed,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def discover_nonempty_semesters(
    client: NJUCourseClient,
    semesters: Iterable[str],
) -> dict[str, PageData]:
    discovered: dict[str, PageData] = {}
    for semester in semesters:
        print(f"Probing {semester}...", flush=True)
        page = client.fetch_page(semester, 1)
        if page.rows:
            discovered[semester] = page
            print(f"  found {len(page.rows)} rows on page 1", flush=True)
        else:
            print("  no rows", flush=True)
    return discovered


def parse_terms(value: str) -> tuple[int, ...]:
    try:
        terms = tuple(
            sorted({int(item.strip()) for item in value.split(",") if item.strip()})
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "terms must be comma-separated integers"
        ) from exc
    if not terms or any(term < 1 for term in terms):
        raise argparse.ArgumentTypeError("terms must contain positive integers")
    return terms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch raw NJU school-wide course-query responses"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all", action="store_true", help="refresh every non-empty semester"
    )
    mode.add_argument(
        "--latest",
        type=int,
        metavar="COUNT",
        help="refresh the latest COUNT non-empty semesters",
    )
    mode.add_argument(
        "--semester",
        action="append",
        dest="semesters",
        metavar="CODE",
        help="refresh one semester; may be repeated",
    )

    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="output directory (default: data)",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=int(os.getenv("NJU_START_YEAR", DEFAULT_START_YEAR)),
        help=f"first academic start year for --all (default: {DEFAULT_START_YEAR})",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="last academic start year to probe",
    )
    parser.add_argument(
        "--terms",
        type=parse_terms,
        default=parse_terms(os.getenv("NJU_SEMESTER_TERMS", "1,2")),
        help="comma-separated semester suffixes (default: 1,2)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.getenv("NJU_REQUEST_DELAY", "1.5")),
        help="minimum seconds between course requests (default: 1.5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("NJU_REQUEST_TIMEOUT", "30")),
        help="request timeout in seconds (default: 30)",
    )
    return parser


def select_work(
    args: argparse.Namespace,
    client: NJUCourseClient,
    today: date | None = None,
) -> dict[str, PageData | None]:
    if args.semesters:
        selected: dict[str, PageData | None] = {}
        for semester in args.semesters:
            semester_sort_key(semester)
            selected[semester] = None
        return dict(
            sorted(selected.items(), key=lambda item: semester_sort_key(item[0]))
        )

    academic_year = current_academic_start_year(today)
    end_year = args.end_year if args.end_year is not None else academic_year + 1

    if args.all:
        candidates = candidate_semesters(args.start_year, end_year, args.terms)
        return discover_nonempty_semesters(client, candidates)

    if args.latest is None or args.latest < 1:
        raise ScrapeError("--latest must be at least 1")

    # A small rolling window avoids probing every historical semester each day.
    start_year = max(args.start_year, academic_year - 2)
    candidates = candidate_semesters(start_year, end_year, args.terms)
    discovered = discover_nonempty_semesters(client, candidates)
    ordered = sorted(discovered, key=semester_sort_key)
    if len(ordered) < args.latest:
        raise ScrapeError(
            f"Only found {len(ordered)} non-empty semesters; "
            f"cannot select the latest {args.latest}"
        )
    return {semester: discovered[semester] for semester in ordered[-args.latest :]}


def run(args: argparse.Namespace) -> list[SemesterResult]:
    castgc = extract_castgc(os.getenv("NJU_CASTGC", ""))
    client = NJUCourseClient(
        castgc,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
    )

    print("Authenticating with NJU CAS...", flush=True)
    client.authenticate()
    print("Authenticated; discovering semesters...", flush=True)

    work = select_work(args, client)
    semesters = sorted(work, key=semester_sort_key)
    print(f"Selected {len(semesters)} semester(s): {', '.join(semesters)}", flush=True)

    results: list[SemesterResult] = []
    for semester in semesters:
        print(f"Scraping {semester}...", flush=True)
        result = scrape_semester(client, args.data_dir, semester, work[semester])
        results.append(result)
        state = "updated" if result.changed else "unchanged"
        print(
            f"  {state}: {result.pages} pages, {result.rows} rows",
            flush=True,
        )

    staging_root = args.data_dir / ".staging"
    if staging_root.exists() and not any(staging_root.iterdir()):
        staging_root.rmdir()

    return results


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        results = run(args)
    except (ScrapeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    changed = sum(result.changed for result in results)
    print(
        f"Done: {len(results)} semester(s), {changed} changed, "
        f"{len(results) - changed} unchanged",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
