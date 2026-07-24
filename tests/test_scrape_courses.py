from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pytest
import requests

from scripts.scrape_courses import (
    AuthenticationError,
    PageData,
    ResponseFormatError,
    candidate_semesters,
    extract_castgc,
    make_query_setting,
    parse_course_response,
    scrape_semester,
    select_work,
    semester_display,
)


class FakeClient:
    def __init__(self, pages: dict[tuple[str, int], PageData]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    def fetch_page(self, semester: str, page: int) -> PageData:
        self.calls.append((semester, page))
        return self.pages.get(
            (semester, page),
            PageData(raw=b'{"datas":{"qxfbkccx":{"rows":[]}}}', rows=[]),
        )


def response_with(
    body: bytes, *, status: int = 200, url: str = "https://ehallapp.nju.edu.cn/api"
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body
    response.headers["Content-Type"] = "application/json"
    return response


def test_extract_castgc_accepts_bare_and_cookie_header() -> None:
    assert extract_castgc("ticket-value") == "ticket-value"
    assert extract_castgc("foo=bar; CASTGC=ticket-value; baz=qux") == "ticket-value"


def test_semester_helpers() -> None:
    assert semester_display("2026-2027-1") == "2026-2027学年 第1学期"
    assert candidate_semesters(2025, 2026, (2, 1)) == [
        "2025-2026-1",
        "2025-2026-2",
        "2026-2027-1",
        "2026-2027-2",
    ]


def test_query_setting_contains_requested_semester() -> None:
    payload = json.loads(make_query_setting("2026-2027-1"))
    assert payload[0]["value"] == "2026-2027-1"
    assert payload[0]["value_display"] == "2026-2027学年 第1学期"


def test_parse_course_response_preserves_raw_bytes() -> None:
    raw = b'{"datas":{"qxfbkccx":{"rows":[{"KCH":"001"}]}}}\n'
    page = parse_course_response(response_with(raw), "2026-2027-1", 1)
    assert page.raw == raw
    assert page.rows == [{"KCH": "001"}]


def test_parse_course_response_treats_login_html_as_auth_failure() -> None:
    response = response_with(b"<!doctype html><html>login</html>")
    with pytest.raises(AuthenticationError):
        parse_course_response(response, "2026-2027-1", 1)


def test_parse_course_response_rejects_ignored_semester_filter() -> None:
    raw = b'{"datas":{"qxfbkccx":{"rows":[{"XNXQDM":"2025-2026-2"}]}}}'
    with pytest.raises(ResponseFormatError, match="returned rows for"):
        parse_course_response(response_with(raw), "2026-2027-1", 1)


def test_scrape_semester_writes_pages_and_removes_stale_pages(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    old = tmp_path / semester
    old.mkdir()
    (old / "page_001.json").write_bytes(b"old")
    (old / "page_002.json").write_bytes(b"stale")

    raw = b'{"datas":{"qxfbkccx":{"rows":[{"KCH":"001"}]}}}'
    first_page = PageData(raw=raw, rows=[{"KCH": "001"}])
    result = scrape_semester(FakeClient({}), tmp_path, semester, first_page)

    assert result.changed is True
    assert result.pages == 1
    assert result.rows == 1
    assert (tmp_path / semester / "page_001.json").read_bytes() == raw
    assert not (tmp_path / semester / "page_002.json").exists()


def test_scrape_semester_reports_unchanged_directory(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    raw = b'{"datas":{"qxfbkccx":{"rows":[{"KCH":"001"}]}}}'
    destination = tmp_path / semester
    destination.mkdir()
    (destination / "page_001.json").write_bytes(raw)

    result = scrape_semester(
        FakeClient({}),
        tmp_path,
        semester,
        PageData(raw=raw, rows=[{"KCH": "001"}]),
    )

    assert result.changed is False
    assert (destination / "page_001.json").read_bytes() == raw


def test_scrape_semester_fetches_until_short_page(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    first = PageData(raw=b"first", rows=[{}] * 500)
    second = PageData(raw=b"second", rows=[{}])
    client = FakeClient({(semester, 2): second})

    result = scrape_semester(client, tmp_path, semester, first)

    assert result.pages == 2
    assert result.rows == 501
    assert client.calls == [(semester, 2)]
    assert (tmp_path / semester / "page_001.json").read_bytes() == b"first"
    assert (tmp_path / semester / "page_002.json").read_bytes() == b"second"


def test_select_latest_uses_only_recent_probe_window() -> None:
    latest = {
        ("2025-2026-2", 1): PageData(raw=b"a", rows=[{}]),
        ("2026-2027-1", 1): PageData(raw=b"b", rows=[{}]),
    }
    client = FakeClient(latest)
    args = argparse.Namespace(
        semesters=None,
        all=False,
        latest=2,
        start_year=2000,
        end_year=None,
        terms=(1, 2),
    )

    selected = select_work(args, client, today=date(2026, 7, 24))

    assert list(selected) == ["2025-2026-2", "2026-2027-1"]
    probed_years = {int(semester[:4]) for semester, _ in client.calls}
    assert min(probed_years) == 2024
    assert max(probed_years) == 2027
