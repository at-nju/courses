from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import pytest
import requests

from scripts.scrape_courses import (
    APP_CONFIG_URL_TEMPLATE,
    APP_ENTRY_URL,
    SET_COMMON_ROLE_URL,
    AuthenticationError,
    NJUCourseClient,
    PageData,
    ResponseFormatError,
    extract_castgc,
    make_query_setting,
    parse_course_response,
    parse_init_config,
    parse_semester_code_url,
    parse_semester_list,
    scrape_semester,
    select_work,
    semester_display,
    write_course_exports,
)


class FakeClient:
    def __init__(
        self,
        pages: dict[tuple[str, int], PageData],
        semesters: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.semesters = semesters or {}
        self.calls: list[tuple[str, int]] = []

    def list_semesters(self) -> dict[str, str]:
        return self.semesters

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
    assert semester_display("2025-2026-3") == "2025-2026学年 暑期"


def test_query_setting_contains_requested_semester() -> None:
    payload = json.loads(make_query_setting("2026-2027-1"))
    assert payload[0]["value"] == "2026-2027-1"
    assert payload[0]["value_display"] == "2026-2027学年 第1学期"


def test_parse_init_config_extracts_app_and_role() -> None:
    html = (
        "<script>window._JW_INIT_CONFIG = "
        '{"appname":"kcbcx","appId":"123","ROLEID":null};</script>'
    )
    assert parse_init_config(html) == ("kcbcx", "123", "")


def test_parse_semester_discovery_metadata() -> None:
    model_payload = {
        "models": [
            {
                "name": "qxfbkccx",
                "controls": [
                    {
                        "name": "XNXQDM",
                        "url": "/jwapp/code/semester-list.do",
                    }
                ],
            }
        ]
    }
    assert parse_semester_code_url(model_payload) == (
        "https://ehallapp.nju.edu.cn/jwapp/code/semester-list.do"
    )

    list_payload = {
        "datas": {
            "code": {
                "rows": [
                    {"id": "2025-2026-3", "name": "2025-2026学年 暑期"},
                    {"id": "2025-2026-2", "name": "2025-2026学年 第2学期"},
                ]
            }
        }
    }
    assert parse_semester_list(list_payload) == {
        "2025-2026-3": "2025-2026学年 暑期",
        "2025-2026-2": "2025-2026学年 第2学期",
    }


def test_authenticate_initializes_app_permissions() -> None:
    session = requests.Session()
    entry = response_with(
        (
            b"<script>window._JW_INIT_CONFIG = "
            b'{"appname":"kcbcx","appId":"123","ROLEID":null};</script>'
        ),
        url=APP_ENTRY_URL,
    )
    app_config = response_with(
        b'{"MODULES":[{"route":"qxkcb"}]}',
        url=APP_CONFIG_URL_TEMPLATE.format(appname="kcbcx", appid="123"),
    )
    role = response_with(b'{"success":"1"}', url=SET_COMMON_ROLE_URL)
    get_calls: list[str] = []
    post_calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, **_: object) -> requests.Response:
        get_calls.append(url)
        return entry if url == APP_ENTRY_URL else app_config

    def fake_post(url: str, data: dict[str, str], **_: object) -> requests.Response:
        post_calls.append((url, data))
        return role

    session.get = fake_get  # type: ignore[method-assign]
    session.post = fake_post  # type: ignore[method-assign]

    client = NJUCourseClient("ticket-value", session=session)
    client.authenticate()

    assert get_calls == [
        APP_ENTRY_URL,
        APP_CONFIG_URL_TEMPLATE.format(appname="kcbcx", appid="123"),
    ]
    assert post_calls == [(SET_COMMON_ROLE_URL, {"ROLEID": ""})]


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


def test_scrape_semester_writes_csv_and_deterministic_gzip(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    rows = [
        {
            "KCH": "001",
            "KCM": '课程, "A"',
            "BZ": "第一行\n第二行",
            "RS": None,
        }
    ]
    page = PageData(raw=b"raw-json", rows=rows)

    result = scrape_semester(FakeClient({}), tmp_path, semester, page)

    expected = ('KCH,KCM,BZ,RS\n001,"课程, ""A""","第一行\n第二行",\n').encode()
    semester_dir = tmp_path / semester
    csv_bytes = (semester_dir / "courses.csv").read_bytes()
    gzip_bytes = (semester_dir / "courses.csv.gz").read_bytes()
    assert result.rows == 1
    assert csv_bytes == expected
    assert gzip.decompress(gzip_bytes) == expected

    second_dir = tmp_path / "second"
    scrape_semester(FakeClient({}), second_dir, semester, page)
    assert (second_dir / semester / "courses.csv.gz").read_bytes() == gzip_bytes


def test_scrape_semester_reports_unchanged_directory(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    raw = b'{"datas":{"qxfbkccx":{"rows":[{"KCH":"001"}]}}}'
    destination = tmp_path / semester
    destination.mkdir()
    (destination / "page_001.json").write_bytes(raw)
    write_course_exports(destination, [{"KCH": "001"}])

    result = scrape_semester(
        FakeClient({}),
        tmp_path,
        semester,
        PageData(raw=raw, rows=[{"KCH": "001"}]),
    )

    assert result.changed is False
    assert (destination / "page_001.json").read_bytes() == raw


def test_scrape_semester_preserves_empty_first_page(tmp_path: Path) -> None:
    semester = "2020-2021-3"
    raw = b'{"datas":{"qxfbkccx":{"rows":[]}}}'

    result = scrape_semester(
        FakeClient({}),
        tmp_path,
        semester,
        PageData(raw=raw, rows=[]),
    )

    assert result.pages == 1
    assert result.rows == 0
    assert result.changed is True
    assert (tmp_path / semester / "page_001.json").read_bytes() == raw
    assert not (tmp_path / semester / "courses.csv").exists()
    assert not (tmp_path / semester / "courses.csv.gz").exists()


def test_scrape_semester_rejects_inconsistent_fields_without_replacing_old_data(
    tmp_path: Path,
) -> None:
    semester = "2026-2027-1"
    destination = tmp_path / semester
    destination.mkdir()
    (destination / "sentinel").write_text("old")
    page = PageData(
        raw=b"raw-json",
        rows=[{"KCH": "001", "KCM": "A"}, {"KCH": "002"}],
    )

    with pytest.raises(ResponseFormatError, match="inconsistent fields"):
        scrape_semester(FakeClient({}), tmp_path, semester, page)

    assert (destination / "sentinel").read_text() == "old"
    assert not list((tmp_path / ".staging").glob(f"{semester}-*"))


def test_scrape_semester_fetches_until_short_page(tmp_path: Path) -> None:
    semester = "2026-2027-1"
    first = PageData(raw=b"first", rows=[{"KCH": "001"}] * 500)
    second = PageData(raw=b"second", rows=[{"KCH": "002"}])
    client = FakeClient({(semester, 2): second})

    result = scrape_semester(client, tmp_path, semester, first)

    assert result.pages == 2
    assert result.rows == 501
    assert client.calls == [(semester, 2)]
    assert (tmp_path / semester / "page_001.json").read_bytes() == b"first"
    assert (tmp_path / semester / "page_002.json").read_bytes() == b"second"


def test_select_latest_uses_authoritative_order_and_stops_after_count() -> None:
    semesters = {
        "2026-2027-1": "2026-2027学年 第1学期",
        "2025-2026-3": "2025-2026学年 暑期",
        "2025-2026-2": "2025-2026学年 第2学期",
    }
    latest = {
        ("2026-2027-1", 1): PageData(raw=b"b", rows=[{}]),
        ("2025-2026-3", 1): PageData(raw=b"a", rows=[{}]),
        ("2025-2026-2", 1): PageData(raw=b"old", rows=[{}]),
    }
    client = FakeClient(latest, semesters)
    args = argparse.Namespace(
        semesters=None,
        all=False,
        latest=2,
        start_year=2000,
        end_year=None,
    )

    selected = select_work(args, client)

    assert list(selected) == ["2025-2026-3", "2026-2027-1"]
    assert client.calls == [("2026-2027-1", 1), ("2025-2026-3", 1)]


def test_select_all_uses_authoritative_semester_list_including_summer() -> None:
    semesters = {
        "2025-2026-3": "2025-2026学年 暑期",
        "2025-2026-2": "2025-2026学年 第2学期",
        "2025-2026-1": "2025-2026学年 第1学期",
    }
    pages = {
        ("2025-2026-1", 1): PageData(raw=b"first", rows=[{"XNXQDM": "2025-2026-1"}]),
        ("2025-2026-2", 1): PageData(raw=b"second", rows=[{"XNXQDM": "2025-2026-2"}]),
        ("2025-2026-3", 1): PageData(raw=b"summer-empty", rows=[]),
    }
    client = FakeClient(pages, semesters)
    args = argparse.Namespace(
        semesters=None,
        all=True,
        latest=None,
        start_year=2025,
        end_year=2025,
    )

    selected = select_work(args, client)

    assert list(selected) == [
        "2025-2026-1",
        "2025-2026-2",
        "2025-2026-3",
    ]
