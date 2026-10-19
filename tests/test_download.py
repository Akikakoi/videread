"""download 模块用例（全部离线）：分P 链接改写与分P 列表探测的降级行为。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from videread import download
from videread.config import Settings
from videread.errors import DownloadError

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


# ------------------------------------------------- 充电专属 / 试看预览拦截


def _view_client(payload: object):
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

    return _FakeClient


def _view_payload(
    rights: dict, full: float, pages: list[dict] | None = None
) -> dict:
    data: dict = {
        "bvid": "BV1xx411c7mD",
        "title": "充电专属课",
        "duration": full,
        "rights": rights,
        "owner": {"name": "某UP主"},
    }
    if pages is not None:
        data["pages"] = pages
    return {"code": 0, "data": data}


def _fake_yt_dlp(duration: float):
    def _run(
        args: list[str], settings: object, *, timeout: float = 900
    ) -> "subprocess.CompletedProcess[str]":
        info = json.dumps(
            {
                "id": "BV1xx411c7mD",
                "title": "充电专属课",
                "duration": duration,
                "uploader": "某UP主",
                "webpage_url": URL,
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout=info, stderr="")

    return _run


def test_charging_exclusive_reads_rights_flag(monkeypatch: pytest.MonkeyPatch):
    """rights 的 is_charging_arc / ugc_pay 任一置位即判为付费内容；API 失败为 None。"""
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 1}, 3000)),
    )
    assert download.charging_exclusive(URL, _settings()) is True

    monkeypatch.setattr(
        download.httpx, "Client", _view_client(_view_payload({"ugc_pay": 1}, 3000))
    )
    assert download.charging_exclusive(URL, _settings()) is True

    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 0, "ugc_pay": 0}, 3000)),
    )
    assert download.charging_exclusive(URL, _settings()) is False

    broken = _view_payload({}, 3000)
    broken["code"] = -404
    monkeypatch.setattr(download.httpx, "Client", _view_client(broken))
    assert download.charging_exclusive(URL, _settings()) is None


def test_fetch_video_info_surfaces_charging_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 1}, 3000)),
    )
    info = download.fetch_video_info(URL, _settings())
    assert info is not None and info.charging is True


def test_fetch_meta_rejects_charging_without_cookies(
    monkeypatch: pytest.MonkeyPatch,
):
    """充电专属且未配登录态：直接拒绝，不应触达 yt-dlp。"""
    no_cookies = replace(
        _settings(), cookies_file=None, cookies_from_browser=None
    )
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 1}, 3000)),
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("充电专属且未配登录态时不应触达 yt-dlp")

    monkeypatch.setattr(download, "_yt_dlp", _boom)
    with pytest.raises(DownloadError, match="充电专属"):
        download.fetch_meta(URL, no_cookies)


def test_fetch_meta_allows_charging_with_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """配了登录态则放行——已充电用户可以完整解析。"""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 1}, 3000)),
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(3000))
    settings = download.get_settings(cookies_file=str(cookies))

    meta = download.fetch_meta(URL, settings)

    assert meta.duration == 3000.0


def test_fetch_meta_rejects_preview_duration(monkeypatch: pytest.MonkeyPatch):
    """rights 未置位但 yt-dlp 时长 ≪ 页面完整时长：判为试看预览并拒绝。"""
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(_view_payload({"is_charging_arc": 0}, 2730)),
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(192))
    with pytest.raises(DownloadError, match="试看内容"):
        download.fetch_meta(URL, _settings())


def test_fetch_meta_multi_page_compares_selected_page(
    monkeypatch: pytest.MonkeyPatch,
):
    """多P 的对照基准是所选分P 的时长，不是全片时长，否则会误判成预览。"""
    pages = [
        {"page": 1, "part": "一", "duration": 600},
        {"page": 2, "part": "二", "duration": 600},
        {"page": 3, "part": "三", "duration": 300},
    ]
    url = URL + "?p=3"

    monkeypatch.setattr(
        download.httpx, "Client", _view_client(_view_payload({}, 1500, pages))
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(290))
    meta = download.fetch_meta(url, _settings())
    assert meta.duration == 290.0

    monkeypatch.setattr(
        download.httpx, "Client", _view_client(_view_payload({}, 1500, pages))
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(60))
    with pytest.raises(DownloadError, match="试看内容"):
        download.fetch_meta(url, _settings())


# ------------------------------------------------- 付费课程（Cheese）拦截

CHEESE_URL = "https://www.bilibili.com/cheese/play/ep2564130"


def test_canonical_url_strips_cheese_tracking_params():
    """csource / spm_id_from 等追踪参数不参与 canonical_url，避免缓存劈成两半。"""
    url = CHEESE_URL + "?csource=Hp_searchresult&spm_id_from=333.337.0.0"
    assert download.canonical_url(url) == CHEESE_URL


def test_make_run_id_uses_ep_prefix_for_cheese():
    run_id = download.make_run_id(CHEESE_URL, download.extract_bvid(CHEESE_URL))
    assert run_id.startswith("ep2564130-")


def test_fetch_meta_rejects_cheese_without_cookies(
    monkeypatch: pytest.MonkeyPatch,
):
    """付费课程未购买集（playable=False）且未配登录态：直接拒绝。"""
    no_cookies = replace(_settings(), cookies_file=None, cookies_from_browser=None)
    payload = _view_client(
        {
            "code": 0,
            "data": {
                "title": "剪辑课",
                "episodes": [
                    {"id": 2564150, "playable": True, "duration": 113},
                    {"id": 2564130, "playable": False, "duration": 306, "ep_status": 0},
                ],
            },
        }
    )
    monkeypatch.setattr(download.httpx, "Client", payload)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("未购买的付费课程不应触达 yt-dlp")

    monkeypatch.setattr(download, "_yt_dlp", _boom)
    with pytest.raises(DownloadError, match="付费课程"):
        download.fetch_meta(CHEESE_URL, no_cookies)


def test_fetch_meta_allows_cheese_free_episode(monkeypatch: pytest.MonkeyPatch):
    """免费试看集（playable=True）：匿名也可完整解析，正常构建元信息。"""
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(
            {
                "code": 0,
                "data": {
                    "title": "剪辑课",
                    "episodes": [
                        {"id": 2564150, "playable": True, "duration": 113, "ep_status": 0},
                    ],
                },
            }
        ),
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(113))
    meta = download.fetch_meta(CHEESE_URL, _settings())
    assert meta.duration == 113.0


# ------------------------------------------------- 付费课程（Cheese）拦截

CHEESE_URL = "https://www.bilibili.com/cheese/play/ep2564130"


def test_canonical_url_strips_cheese_tracking_params():
    """csource / spm_id_from 等追踪参数不参与 canonical_url，避免缓存劈成两半。"""
    url = CHEESE_URL + "?csource=Hp_searchresult&spm_id_from=333.337.0.0"
    assert download.canonical_url(url) == CHEESE_URL


def test_make_run_id_uses_ep_prefix_for_cheese():
    run_id = download.make_run_id(CHEESE_URL, download.extract_bvid(CHEESE_URL))
    assert run_id.startswith("ep2564130-")


def test_fetch_meta_rejects_cheese_without_cookies(
    monkeypatch: pytest.MonkeyPatch,
):
    """付费课程未购买集（playable=False）且未配登录态：直接拒绝。"""
    no_cookies = replace(_settings(), cookies_file=None, cookies_from_browser=None)
    payload = _view_client(
        {
            "code": 0,
            "data": {
                "title": "剪辑课",
                "episodes": [
                    {"id": 2564150, "playable": True, "duration": 113},
                    {"id": 2564130, "playable": False, "duration": 306, "ep_status": 0},
                ],
            },
        }
    )
    monkeypatch.setattr(download.httpx, "Client", payload)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("未购买的付费课程不应触达 yt-dlp")

    monkeypatch.setattr(download, "_yt_dlp", _boom)
    with pytest.raises(DownloadError, match="付费课程"):
        download.fetch_meta(CHEESE_URL, no_cookies)


def test_fetch_meta_allows_cheese_free_episode(monkeypatch: pytest.MonkeyPatch):
    """免费试看集（playable=True）：匿名也可完整解析，正常构建元信息。"""
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(
            {
                "code": 0,
                "data": {
                    "title": "剪辑课",
                    "episodes": [
                        {"id": 2564150, "playable": True, "duration": 113, "ep_status": 0},
                    ],
                },
            }
        ),
    )
    monkeypatch.setattr(download, "_yt_dlp", _fake_yt_dlp(113))
    meta = download.fetch_meta(CHEESE_URL, _settings())
    assert meta.duration == 113.0


def test_fetch_meta_translates_cheese_purchase_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """配了 cookies 但未购买该课程：yt-dlp 的英文购买报错应换成中文拒绝文案。"""
    settings = _settings()  # 本机 .env 配有登录态
    monkeypatch.setattr(
        download.httpx,
        "Client",
        _view_client(
            {
                "code": 0,
                "data": {
                    "title": "剪辑课",
                    "episodes": [
                        {"id": 2564130, "playable": False, "duration": 306, "ep_status": 0},
                    ],
                },
            }
        ),
    )

    def _fail_yt_dlp(args: list[str], settings: object, *, timeout: float = 900):
        return subprocess.CompletedProcess(
            args, 1, stdout="null", stderr="ERROR: You need to purchase the course"
        )

    monkeypatch.setattr(download, "_yt_dlp", _fail_yt_dlp)
    with pytest.raises(DownloadError, match="付费课程"):
        download.fetch_meta(CHEESE_URL, settings)
