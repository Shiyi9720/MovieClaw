"""跳过片头 / 片尾的服务端链路（docs/design/skip-intro.md）。

ffmpeg 的 chromaprint 用替身（按文件路径生成可复现的合成指纹：每集片头位置不同、
片尾放到结尾），其余全是真的：整季识别在独立子进程里跑、结果落 ``media_segment``、
播放会话下发、Jellyfin ``/MediaSegments`` 输出、库开关收放、后台作业走真执行器。
"""

from __future__ import annotations

import asyncio
import re
import time
from functools import partial
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from jellyfin.helpers import AUTH_HEADER
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.library import skip_segments
from movieclaw_api.services.playback.session import get_session_manager, reset_session_manager
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, MediaSegmentState
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_jellyfin.ids import episode_guid
from movieclaw_playback import activity
from movieclaw_playback.events import ClientInfo
from movieclaw_playback.skip_segments import HASH_SECONDS

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}
CAPABILITY = {
    "video": [{"codec": "h264"}],
    "audio": [{"codec": "aac"}],
    "containers": ["mp4", "hls-fmp4", "matroska"],
}
DURATION = 2700
INTRO_S = 60.0
OUTRO_S = 120.0


def intro_at(episode: int) -> float:
    """第 n 集片头的起点：冷开场长短不一。"""
    return 20.0 + 15.0 * episode


_COMMON = np.random.default_rng(2026)
_INTRO = _COMMON.integers(0, 2**32, size=int(INTRO_S / HASH_SECONDS), dtype=np.uint64).astype(
    np.uint32
)
_OUTRO = _COMMON.integers(0, 2**32, size=int(OUTRO_S / HASH_SECONDS), dtype=np.uint64).astype(
    np.uint32
)


async def fake_window(path: str, start: float, length: float) -> bytes:
    """合成指纹：随机底噪 + 片头窗贴片头、片尾窗在末尾贴片尾。"""
    if "bad" in path:
        raise skip_segments.FingerprintError("文件没有音轨")
    match = re.search(r"E(\d+)", Path(path).name)
    episode = int(match.group(1)) if match else 0
    rng = np.random.default_rng(abs(hash((path, round(start)))) % (2**32))
    out = rng.integers(0, 2**32, size=int(length / HASH_SECONDS), dtype=np.uint64).astype(np.uint32)
    if start == 0:
        i = int(intro_at(episode) / HASH_SECONDS)
        out[i : i + len(_INTRO)] = _INTRO
    else:
        out[len(out) - len(_OUTRO) :] = _OUTRO
    return out.astype("<u4").tobytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MOVIECLAW_TRANSCODE_DIR", str(tmp_path / "transcodes"))
    monkeypatch.setenv("MOVIECLAW_AUDIO_FINGERPRINT_DIR", str(tmp_path / "fp"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_session_manager()
    monkeypatch.setattr(skip_segments, "_chromaprint_window", fake_window)
    monkeypatch.setattr(skip_segments, "_chromaprint", True)
    monkeypatch.setattr(skip_segments, "_BUMP_DELAY_S", 0.0)
    monkeypatch.setattr("movieclaw_api.api.routes.playback.available_backends", lambda: ())
    monkeypatch.setattr(
        "movieclaw_api.services.playback.plan.probe_keyframe_interval",
        lambda path, duration: 4.0,
    )
    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        get_session_manager().shutdown()
    )
    reset_session_manager()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


async def _seed(tmp_path: Path, episodes: int = 5, *, bad: int | None = None) -> dict:
    """剧集库（1 部剧第 1 季 n 集 + 第 0 季特别篇 + 一个 strm）+ 电影库（1 部片）。"""
    root = tmp_path / "tv"
    movies = tmp_path / "movies"
    root.mkdir(exist_ok=True)
    movies.mkdir(exist_ok=True)
    async with get_database().session() as session:
        repo = LibraryRepository(session)
        tv = await repo.create(name="剧集", kind="tv", root_paths=[str(root)])
        movie_lib = await repo.create(name="电影", kind="movie", root_paths=[str(movies)])
        show = MediaItem(kind="tv", tmdb_id=1, title="示例剧", original_title="Show")
        film = MediaItem(kind="movie", tmdb_id=2, title="示例片", original_title="Film")
        session.add_all([show, film])
        await session.flush()

        def row(library_id, item_id, path: Path, season, episode, container="mp4"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"FAKE-MEDIA" * 64)
            return LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=season,
                episode_number=episode,
                file_path=str(path),
                size_bytes=path.stat().st_size,
                source=FileSource.SCANNED,
                state=FileState.IN_PLACE,
                container=container,
                video_codec="h264",
                resolution="1080p",
                duration_seconds=DURATION,
                audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
            )

        files = []
        for n in range(1, episodes + 1):
            name = f"Show S01E{n:02d}{'.bad' if n == bad else ''}.mp4"
            files.append(row(tv.id, show.id, root / "Season 01" / name, 1, n))
        special = row(tv.id, show.id, root / "Specials" / "Show S00E01.mp4", 0, 1)
        film_row = row(movie_lib.id, film.id, movies / "Film.mp4", 0, 0)
        session.add_all([*files, special, film_row])
        await session.commit()
        return {
            "tv": tv.id,
            "movie_lib": movie_lib.id,
            "show": show.id,
            "files": [f.id for f in files],
            "special": special.id,
            "film": film_row.id,
        }


def call(client: TestClient, fn, *args, **kwargs):
    return client.portal.call(partial(fn, *args, **kwargs))  # type: ignore[attr-defined]


async def _states() -> dict[int, MediaSegmentState]:
    async with get_database().session() as session:
        rows = (await session.execute(select(MediaSegmentState))).scalars().all()
        return {r.library_file_id: r for r in rows}


async def _needing(**kwargs) -> list[tuple[int, int]]:
    async with get_database().session() as session:
        return await skip_segments.seasons_needing_work(session, **kwargs)


def start_session(client: TestClient, file_id: int) -> dict:
    resp = client.post(f"{_PB}/sessions", json={"file_id": file_id, "capability": CAPABILITY})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def jf_token(client: TestClient) -> str:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": _ADMIN["username"], "Pw": _ADMIN["password"]},
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["AccessToken"]


def test_only_eligible_episodes_need_work(client: TestClient, tmp_path: Path) -> None:
    """电影、第 0 季不参与；开关关掉的库不参与。"""
    ids = call(client, _seed, tmp_path)
    assert call(client, _needing) == [(ids["show"], 1)]
    assert call(client, _needing, library_id=ids["movie_lib"]) == []


def test_season_analysis_feeds_session_and_jellyfin(client: TestClient, tmp_path: Path) -> None:
    ids = call(client, _seed, tmp_path)
    outcome = call(client, skip_segments.analyze_season, ids["show"], 1)
    assert outcome.fingerprinted == 5 and outcome.analyzed
    assert outcome.with_intro == 5 and outcome.with_outro == 5
    for file_id in ids["files"]:
        assert (Path(tmp_path / "fp") / f"{file_id}.fp").is_file()
    # 做完的季不再有活
    assert call(client, _needing) == []

    # 播放会话随响应下发片段
    third = ids["files"][2]
    session = start_session(client, third)
    segments = {s["type"]: s for s in session["segments"]}
    assert set(segments) == {"intro", "outro"}
    assert abs(segments["intro"]["start_ms"] - intro_at(3) * 1000) < 1500
    assert abs(segments["intro"]["end_ms"] - (intro_at(3) + INTRO_S) * 1000) < 1500
    assert segments["outro"]["to_end"] is True
    assert abs(segments["outro"]["end_ms"] - DURATION * 1000) < 1500
    # 电影没有片段
    assert start_session(client, ids["film"])["segments"] == []

    # Jellyfin：按集给，Ticks = 毫秒 × 10000，可按类型过滤
    token = jf_token(client)
    guid = episode_guid(ids["show"], 1, 3)
    body = client.get(f"/MediaSegments/{guid}", params={"ApiKey": token}).json()
    assert [i["Type"] for i in body["Items"]] == ["Intro", "Outro"]
    assert body["TotalRecordCount"] == 2
    assert body["Items"][0]["StartTicks"] == segments["intro"]["start_ms"] * 10_000
    assert all(i["ItemId"] == guid for i in body["Items"])
    only_outro = client.get(
        f"/MediaSegments/{guid}", params={"ApiKey": token, "includeSegmentTypes": "Outro"}
    ).json()
    assert [i["Type"] for i in only_outro["Items"]] == ["Outro"]
    # 特别篇没识别：空 QueryResult
    special = episode_guid(ids["show"], 0, 1)
    assert client.get(f"/MediaSegments/{special}", params={"ApiKey": token}).json()["Items"] == []


def test_switch_off_hides_segments_and_cancels_job(client: TestClient, tmp_path: Path) -> None:
    ids = call(client, _seed, tmp_path)
    call(client, skip_segments.analyze_season, ids["show"], 1)
    lib = client.get(f"/api/v1/libraries/{ids['tv']}").json()["data"]
    assert lib["detect_media_segments"] is True

    async def _queue_library_job() -> str:
        async with get_database().session() as session:
            created = await skip_segments.enqueue_library_job(session, ids["tv"], "剧集")
            return created.job.id

    payload = {"name": "剧集", "kind": "tv", "root_paths": lib["root_paths"]}
    job_id = call(client, _queue_library_job)
    resp = client.put(
        f"/api/v1/libraries/{ids['tv']}", json={**payload, "detect_media_segments": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["detect_media_segments"] is False

    async def _job_status() -> str:
        async with get_database().session() as session:
            job = await jobs.get_job(session, job_id)
            return str(job.status)

    assert call(client, _job_status) in {"cancelled", "cancelling", "succeeded"}
    assert start_session(client, ids["files"][0])["segments"] == []
    token = jf_token(client)
    guid = episode_guid(ids["show"], 1, 1)
    assert client.get(f"/MediaSegments/{guid}", params={"ApiKey": token}).json()["Items"] == []
    # 不传字段不改动（老客户端）；重新打开立即恢复（结果保留着）
    assert (
        client.put(f"/api/v1/libraries/{ids['tv']}", json=payload).json()["data"][
            "detect_media_segments"
        ]
        is False
    )
    client.put(f"/api/v1/libraries/{ids['tv']}", json={**payload, "detect_media_segments": True})
    assert len(start_session(client, ids["files"][0])["segments"]) == 2


def test_failed_fingerprint_is_recorded_and_retried_after_source_change(
    client: TestClient, tmp_path: Path
) -> None:
    ids = call(client, _seed, tmp_path, 5, bad=2)
    outcome = call(client, skip_segments.analyze_season, ids["show"], 1)
    assert outcome.failed == 1 and outcome.fingerprinted == 4 and outcome.with_intro == 4
    states = call(client, _states)
    bad = states[ids["files"][1]]
    assert bad.fingerprint_status == "failed" and bad.error == "文件没有音轨"
    # 失败是终态：不会每轮重试
    assert call(client, _needing) == []

    async def _replace_source() -> None:
        async with get_database().session() as session:
            row = await session.get(LibraryFile, ids["files"][1])
            row.size_bytes = (row.size_bytes or 0) + 1
            session.add(row)
            await session.commit()

    call(client, _replace_source)
    assert call(client, _needing) == [(ids["show"], 1)]


def test_playback_bump_runs_item_job_through_executor(client: TestClient, tmp_path: Path) -> None:
    """开播时这一季还没算过 → 后台排一份条目作业，真执行器跑完后下一次开播就有片段。"""
    ids = call(client, _seed, tmp_path)
    assert start_session(client, ids["files"][0])["segments"] == []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        states = call(client, _states)
        if len(states) == 5 and all(s.analyzed_at is not None for s in states.values()):
            break
        time.sleep(0.3)
    else:
        pytest.fail("开播后 60 秒内没有跑完片头片尾识别")
    assert {s["type"] for s in start_session(client, ids["files"][0])["segments"]} == {
        "intro",
        "outro",
    }


def test_clearing_fingerprint_cache_recomputes_on_next_analysis(
    client: TestClient, tmp_path: Path
) -> None:
    """缓存管理里清空指纹：识别结果不丢；之后这一季要重算时会把缺的指纹补回来。"""
    ids = call(client, _seed, tmp_path)
    call(client, skip_segments.analyze_season, ids["show"], 1)
    for f in (tmp_path / "fp").iterdir():
        f.unlink()
    assert len(start_session(client, ids["files"][0])["segments"]) == 2

    async def _expire() -> None:
        async with get_database().session() as session:
            row = await session.get(MediaSegmentState, ids["files"][0])
            row.analyzed_at = None
            session.add(row)
            await session.commit()

    call(client, _expire)
    outcome = call(client, skip_segments.analyze_season, ids["show"], 1)
    assert outcome.fingerprinted == 5 and outcome.with_intro == 5


class _Ctx:
    """最小的作业上下文替身：只实现 analyze_season 在作业里会用到的两个方法。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def raise_if_cancelled(self) -> None:
        return None

    async def update_progress(self, **kwargs) -> None:
        self.messages.append(str(kwargs.get("message")))


def test_library_backfill_pauses_while_someone_is_watching(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """整库回填读的是和播放同一份磁盘：有人在看片（未暂停的会话）就先等，暂停 / 放完后继续。"""
    ids = call(client, _seed, tmp_path)
    monkeypatch.setattr(skip_segments, "_QUIET_POLL_S", 0.05)
    device = ClientInfo(name="测试播放器", device_id="dev-1")
    unit = (ids["show"], 1, 1)
    ctx = _Ctx()

    async def scenario():
        activity.reset()
        activity.report_progress(
            "dev-1", member_id=0, client=device, unit=unit, position_ms=1000, paused=False
        )
        assert skip_segments.playback_active()
        task = asyncio.create_task(
            skip_segments.analyze_season(ids["show"], 1, context=ctx, polite=True)
        )
        await asyncio.sleep(0.8)
        paused_all_along = not task.done() and not list((tmp_path / "fp").glob("*.fp"))
        # 用户按了暂停：不再算「在看」，回填放行
        activity.report_progress(
            "dev-1", member_id=0, client=device, unit=unit, position_ms=1000, paused=True
        )
        outcome = await asyncio.wait_for(task, 60)
        activity.reset()
        return paused_all_along, outcome

    paused_all_along, outcome = call(client, scenario)
    assert paused_all_along, "有人在看片时不该读盘算指纹"
    assert outcome.fingerprinted == 5 and outcome.analyzed
    assert any("有人正在看片" in m for m in ctx.messages)


def test_item_job_and_ingest_never_wait_for_playback(client: TestClient, tmp_path: Path) -> None:
    """入库与开播提队的条目作业不让路：只读一集刚下载好的文件 / 本来就因为开播才排。"""
    ids = call(client, _seed, tmp_path)
    device = ClientInfo(name="测试播放器", device_id="dev-2")

    async def scenario():
        activity.reset()
        activity.report_progress(
            "dev-2",
            member_id=0,
            client=device,
            unit=(ids["show"], 1, 1),
            position_ms=0,
            paused=False,
        )
        try:
            return await asyncio.wait_for(skip_segments.analyze_season(ids["show"], 1), 60)
        finally:
            activity.reset()

    outcome = call(client, scenario)
    assert outcome.fingerprinted == 5 and outcome.analyzed
