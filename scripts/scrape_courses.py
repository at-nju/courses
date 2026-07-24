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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

APP_ENTRY_URL = "https://ehallapp.nju.edu.cn/jwapp/sys/kcbcx/*default/index.do"
APP_CONFIG_URL_TEMPLATE = (
    "https://ehallapp.nju.edu.cn/jwapp/sys/funauthapp/api/getAppConfig/"
    "{appname}-{appid}.do"
)
SET_COMMON_ROLE_URL = (
    "https://ehallapp.nju.edu.cn/jwapp/sys/jwpubapp/pub/setJwCommonAppRole.do"
)
COURSE_MODEL_URL = (
    "https://ehallapp.nju.edu.cn/jwapp/sys/kcbcx/modules/qxkcb.do?*json=1"
)
COURSE_API_URL = "https://ehallapp.nju.edu.cn/jwapp/sys/kcbcx/modules/qxkcb/qxfbkccx.do"
PAGE_SIZE = 500
DEFAULT_START_YEAR = 2000
SEMESTER_PATTERN = re.compile(r"^(\d{4})-(\d{4})-(\d+)$")
INIT_CONFIG_PATTERN = re.compile(
    r"window\._JW_INIT_CONFIG\s*=\s*(\{.*?\});",
    re.DOTALL,
)


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
    if term == "3":
        return f"{start}-{end}学年 暑期"
    return f"{start}-{end}学年 第{term}学期"


def semester_sort_key(semester: str) -> tuple[int, int, int]:
    match = SEMESTER_PATTERN.fullmatch(semester)
    if not match:
        raise ValueError(f"Invalid semester code: {semester}")
    return tuple(int(item) for item in match.groups())


def make_query_setting(semester: str, display_name: str | None = None) -> str:
    setting = [
        {
            "name": "XNXQDM",
            "caption": "学年学期",
            "linkOpt": "AND",
            "builderList": "cbl_m_List",
            "builder": "m_value_equal",
            "value": semester,
            "value_display": display_name or semester_display(semester),
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


def parse_init_config(html: str) -> tuple[str, str, str]:
    match = INIT_CONFIG_PATTERN.search(html)
    if not match:
        raise ResponseFormatError("Course application page lacks _JW_INIT_CONFIG")

    try:
        config = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ResponseFormatError(
            "Course application has invalid _JW_INIT_CONFIG"
        ) from exc

    appname = config.get("appname")
    appid = config.get("appId")
    role = config.get("ROLEID")
    if not isinstance(appname, str) or not appname:
        raise ResponseFormatError("Course application config lacks appname")
    if not isinstance(appid, (str, int)) or not str(appid):
        raise ResponseFormatError("Course application config lacks appId")

    return appname, str(appid), "" if role is None else str(role)


def parse_semester_code_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ResponseFormatError("Course module metadata is not an object")

    models = payload.get("models")
    if not isinstance(models, list):
        raise ResponseFormatError("Course module metadata lacks models")

    preferred_models = [
        model
        for model in models
        if isinstance(model, dict) and model.get("name") == "qxfbkccx"
    ]
    other_models = [
        model
        for model in models
        if isinstance(model, dict) and model not in preferred_models
    ]
    candidates = preferred_models + other_models
    for model in candidates:
        controls = model.get("controls")
        if not isinstance(controls, list):
            continue
        for control in controls:
            if not isinstance(control, dict) or control.get("name") != "XNXQDM":
                continue
            code_url = control.get("url")
            if isinstance(code_url, str) and code_url:
                return urljoin(APP_ENTRY_URL, code_url)

    raise ResponseFormatError("Course module metadata lacks the semester code list")


def parse_semester_list(payload: Any) -> dict[str, str]:
    try:
        rows = payload["datas"]["code"]["rows"]
    except (KeyError, TypeError) as exc:
        raise ResponseFormatError(
            "Semester code response lacks datas.code.rows"
        ) from exc

    if not isinstance(rows, list):
        raise ResponseFormatError("Semester code rows is not a list")

    semesters: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ResponseFormatError("Semester code row is not an object")
        code = row.get("id")
        name = row.get("name")
        if not isinstance(code, str) or not SEMESTER_PATTERN.fullmatch(code):
            raise ResponseFormatError(f"Invalid semester code in code list: {code!r}")
        if not isinstance(name, str) or not name:
            raise ResponseFormatError(f"Semester {code} lacks a display name")
        if code in semesters:
            raise ResponseFormatError(f"Duplicate semester code in code list: {code}")
        semesters[code] = name

    if not semesters:
        raise ResponseFormatError("Semester code list is empty")
    return semesters


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
        self.debug = os.getenv("NJU_DEBUG") == "1"
        self._last_request_at: float | None = None
        self.semester_names: dict[str, str] = {}

        self.session.headers.update(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://ehallapp.nju.edu.cn",
                "Referer": APP_ENTRY_URL,
                "Sec-CH-UA": (
                    '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'
                ),
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"macOS"',
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
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

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _debug_session(self, response: requests.Response) -> None:
        if not self.debug:
            return
        chain = [*response.history, response]
        summary = " -> ".join(
            f"{item.status_code} {self._safe_url(str(item.url))}" for item in chain
        )
        cookies = sorted(
            f"{cookie.name}@{cookie.domain}{cookie.path}"
            for cookie in self.session.cookies
        )
        print(f"Authentication redirects: {summary}", flush=True)
        print(f"Session cookie names: {', '.join(cookies) or '(none)'}", flush=True)

    def authenticate(self) -> None:
        try:
            response = self.session.get(
                APP_ENTRY_URL,
                allow_redirects=True,
                timeout=self.timeout_seconds,
            )
            self._debug_session(response)
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

        appname, appid, role = parse_init_config(response.text)
        app_config_url = APP_CONFIG_URL_TEMPLATE.format(
            appname=appname,
            appid=appid,
        )
        try:
            app_config_response = self.session.get(
                app_config_url,
                timeout=self.timeout_seconds,
            )
            app_config_response.raise_for_status()
            app_config = app_config_response.json()
            if not isinstance(app_config, dict):
                raise ResponseFormatError(
                    "Course application authorization config is not an object"
                )
            modules = app_config.get("MODULES")
            if not isinstance(modules, list) or not modules:
                raise AuthenticationError(
                    "NJU account is not authorized for the course application"
                )

            role_response = self.session.post(
                SET_COMMON_ROLE_URL,
                data={"ROLEID": role},
                timeout=self.timeout_seconds,
            )
            role_response.raise_for_status()
            role_result = role_response.json()
            if (
                not isinstance(role_result, dict)
                or str(role_result.get("success")) != "1"
            ):
                raise AuthenticationError(
                    "NJU course application role initialization failed"
                )
        except (requests.JSONDecodeError, ValueError) as exc:
            raise ResponseFormatError(
                "NJU course application initialization returned invalid JSON"
            ) from exc
        except requests.RequestException as exc:
            raise ScrapeError(
                "Failed to initialize the NJU course application"
            ) from exc

    def list_semesters(self) -> dict[str, str]:
        try:
            model_response = self.session.get(
                COURSE_MODEL_URL,
                timeout=self.timeout_seconds,
            )
            model_response.raise_for_status()
            code_url = parse_semester_code_url(model_response.json())

            code_response = self.session.get(
                code_url,
                timeout=self.timeout_seconds,
            )
            code_response.raise_for_status()
            semesters = parse_semester_list(code_response.json())
        except (requests.JSONDecodeError, ValueError) as exc:
            raise ResponseFormatError(
                "NJU semester discovery returned invalid JSON"
            ) from exc
        except requests.RequestException as exc:
            raise ScrapeError("Failed to fetch the NJU semester list") from exc

        self.semester_names = semesters
        return semesters

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
            "querySetting": make_query_setting(
                semester,
                self.semester_names.get(semester),
            ),
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

                if self.debug:
                    print(
                        "Course response: "
                        f"HTTP {response.status_code} "
                        f"{self._safe_url(str(response.url))}; "
                        f"content-type={response.headers.get('content-type', '(missing)')}",
                        flush=True,
                    )

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
    return dict(sorted(discovered.items(), key=lambda item: semester_sort_key(item[0])))


def discover_latest_nonempty_semesters(
    client: NJUCourseClient,
    semesters: Iterable[str],
    count: int,
) -> dict[str, PageData]:
    discovered: dict[str, PageData] = {}
    for semester in sorted(semesters, key=semester_sort_key, reverse=True):
        print(f"Probing {semester}...", flush=True)
        page = client.fetch_page(semester, 1)
        if page.rows:
            discovered[semester] = page
            print(f"  found {len(page.rows)} rows on page 1", flush=True)
            if len(discovered) == count:
                break
        else:
            print("  no rows", flush=True)
    return dict(sorted(discovered.items(), key=lambda item: semester_sort_key(item[0])))


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
        help="last academic start year to include from the official semester list",
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
) -> dict[str, PageData | None]:
    if args.semesters:
        selected: dict[str, PageData | None] = {}
        for semester in args.semesters:
            semester_sort_key(semester)
            selected[semester] = None
        return dict(
            sorted(selected.items(), key=lambda item: semester_sort_key(item[0]))
        )

    semesters = client.list_semesters()
    candidates = [
        semester
        for semester in semesters
        if semester_sort_key(semester)[0] >= args.start_year
        and (args.end_year is None or semester_sort_key(semester)[0] <= args.end_year)
    ]
    if not candidates:
        raise ScrapeError(
            "The official semester list has no semesters in the requested range"
        )

    if args.all:
        return discover_nonempty_semesters(client, candidates)

    if args.latest is None or args.latest < 1:
        raise ScrapeError("--latest must be at least 1")

    discovered = discover_latest_nonempty_semesters(client, candidates, args.latest)
    if len(discovered) < args.latest:
        raise ScrapeError(
            f"Only found {len(discovered)} non-empty semesters; "
            f"cannot select the latest {args.latest}"
        )
    return discovered


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
