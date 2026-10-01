"""download 模块用例（全部离线）：分P 链接改写与分P 列表探测的降级行为。"""

from __future__ import annotations

import pytest

from videread import download
from videread.config import Settings

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


# ---------------------------------------------------------------- page_url


@pytest.mark.parametrize(
    ("url", "page", "expected"),
    [
        # 普通追加
        (URL, 2, URL + "?p=2"),
        # 已有 p 参数则替换，不叠加
        (URL + "?p=2", 3, URL + "?p=3"),
        (URL + "?p=9&t=120", 4, URL + "?t=120&p=4"),
        # p=1 移除参数：裸链接与 ?p=1 必须同一 run-id，缓存不能劈成两半
        (URL + "?p=3", 1, URL),
        (URL + "?p=3&t=120", 1, URL + "?t=120"),
        # page <= 1 且无参数时原样返回
        (URL, 0, URL),
        (URL, -1, URL),
    ],
)
def test_page_url_rewrites_p_param(url: str, page: int, expected: str):
    assert download.page_url(url, page) == expected


def test_page_url_ignores_non_p_query_params():
    url = URL + "?share_source=copy&t=42"
    rewritten = download.page_url(url, 2)
    assert "p=2" in rewritten
    assert "share_source=copy" in rewritten
    assert "t=42" in rewritten


# ------------------------------------------------------------ current_page


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (URL, 1),
        (URL + "?p=2", 2),
        (URL + "?p=abc", 1),  # 非法值按第 1 个分P
        (URL + "?t=120", 1),
    ],
)
def test_current_page_parses_p_param(url: str, expected: int):
    assert download.current_page(url) == expected


# --------------------------------------------------------- fetch_video_info


def _settings() -> Settings:
    return download.get_settings()


@pytest.mark.parametrize(
    "url",
    [
        "https://b23.tv/abc123",          # 短链拿不到 BV 号
        "https://example.com/not-bili",   # 站外链接
        "not-a-url",
    ],
)
def test_fetch_video_info_skips_urls_without_bvid(url: str):
    assert download.fetch_video_info(url, _settings()) is None


def test_fetch_video_info_degrades_to_none_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    """接口异常 / 非 0 返回码 / 结构不符都按「无概要信息」处理，不抛错。"""

    class _FakeResponse:
        def json(self) -> object:
            return {"code": -404, "message": "啥都木有"}

    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, *_args, **_kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(download.httpx, "Client", _FakeClient)
    assert download.fetch_video_info(URL, _settings()) is None


def test_fetch_video_info_parses_view_api_payload(monkeypatch: pytest.MonkeyPatch):
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1xx411c7mD",
            "title": "系列课",
            "owner": {"name": "某UP主"},
            "pages": [
                {"page": 1, "part": "第一集", "duration": 158},
                {"page": 2, "part": "第二集", "duration": 154.6},
                {"broken": True},  # 脏数据项应被跳过
            ],
        },
    }

    class _FakeResponse:
        def json(self) -> object:
            return payload

    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, *_args, **_kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(download.httpx, "Client", _FakeClient)
    info = download.fetch_video_info(URL, _settings())

    assert info == download.VideoInfo(
        title="系列课",
        uploader="某UP主",
        pages=[
            download.VideoPage(page=1, title="第一集", duration=158.0),
            download.VideoPage(page=2, title="第二集", duration=154.6),
        ],
    )
