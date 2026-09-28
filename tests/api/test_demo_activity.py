"""公开演示站的订阅与观看数据（services/demo_activity.py）的不变量。

造出来的数据给访客看，所以守的是「看起来合理」：不出现未来时刻、没入库就看过、
超管看过的片却挂在他的「刚刚入库」里；每天重启重建时不能越堆越多；
「正在播放」同一时刻算出来的结果一致、进度落在片长之内。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import demo_activity
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    PlaybackLog,
    PlaybackState,
    Subscription,
    SubscriptionFollower,
    WantedItem,
)
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.library_repo import LibraryRepository

NOW = datetime(2026, 9, 28, 12, 0)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    async with get_database().session() as session:
        movies = await LibraryRepository(session).create(
            name="电影", kind="movie", root_paths=["/m"]
        )
        await LibraryRepository(session).create(name="图片", kind="photo", root_paths=["/p"])
        items = [
            MediaItem(kind="movie", tmdb_id=100 + i, title=f"影片{i}", original_title=f"F{i}")
            for i in range(6)
        ]
        session.add_all(items)
        session.add(Member(username="family", password_hash="x", nickname="家人"))
        await session.flush()
        session.add_all(
            LibraryFile(
                library_id=movies.id,
                media_item_id=item.id,
                file_path=f"/m/{item.id}.mp4",
                size_bytes=500_000_000,
                source=FileSource.SCANNED,
                duration_seconds=600,
                state=FileState.IN_PLACE,
            )
            for item in items
        )
        await session.commit()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def test_seeded_data_is_plausible_and_idempotent(db) -> None:
    await demo_activity.seed_demo_data(NOW)
    await demo_activity.seed_demo_data(NOW)  # 当天重启：结果不翻倍

    async with db.session() as session:
        subs = (await session.execute(select(Subscription))).scalars().all()
        wanted = {w.media_item_id: w for w in (await session.execute(select(WantedItem))).scalars()}
        logs = (await session.execute(select(PlaybackLog))).scalars().all()
        states = (await session.execute(select(PlaybackState))).scalars().all()
        followers = (await session.execute(select(SubscriptionFollower))).scalars().all()
        family_id = (await session.execute(select(Member.id))).scalar_one()

    assert len(subs) == 6 and all(s.status == "completed" for s in subs)
    assert any(s.created_by_member_id == family_id for s in subs) or followers
    recent = {i for i, w in wanted.items() if NOW - w.imported_at < timedelta(days=7)}
    assert len(recent) == 3

    assert logs, "应该造出播放记录"
    for log in logs:
        assert demo_activity.is_seeded_device(log.device_id)
        assert log.ended_at is not None and log.ended_at < NOW
        assert log.started_at >= wanted[log.media_item_id].imported_at
        if log.member_id == 0:
            assert log.media_item_id not in recent, "超管看过的片不该挂在他的「刚刚入库」里"

    admin_played = {s.media_item_id for s in states if s.member_id == 0 and s.played}
    assert not admin_played & recent
    assert any(s.is_favorite for s in states)
    assert any(s.position_ms > 0 and not s.played for s in states), "「继续观看」需要续播点"


async def test_live_sessions_are_deterministic_and_within_runtime(db) -> None:
    await demo_activity.seed_demo_data(NOW)
    epoch = NOW.timestamp()
    first = demo_activity.live_sessions(epoch)
    again = demo_activity.live_sessions(epoch)
    assert [(s.device_id, s.unit, s.position_ms) for s in first] == [
        (s.device_id, s.unit, s.position_ms) for s in again
    ]
    later = {s.device_id: s for s in demo_activity.live_sessions(epoch + 30)}
    for session in first:
        assert demo_activity.is_seeded_device(session.device_id)
        assert 0 <= (session.position_ms or 0) < 600_000
        follow = later.get(session.device_id)
        if follow is not None and follow.unit == session.unit:
            assert follow.position_ms == (session.position_ms or 0) + 30_000
