"""公开演示站 AI 助手（services/demo_agent.py）的不变量。

演示模型不理解语言，守的是「放进真实 Agent 运行时里不出错、不串号」：
每个预置问题都能完整跑完（工具参数合法、收尾有正文），预置对话的转录协议完整
（每个工具调用都有回执），会话按登录设备隔离、重复补齐不翻倍。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import func, select

from movieclaw_agent import AgentRunner, AgentStartParams
from movieclaw_agent.tools.media_ui import TOOL_NAME as MEDIA_CARDS_TOOL
from movieclaw_agent.tools.media_ui import validate_items
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import demo_activity, demo_agent
from movieclaw_api.services.agent_sessions import (
    get_agent_session_store,
    reset_agent_session_store,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.models.agent_session import AgentSession
from movieclaw_db.models.member import Member
from movieclaw_db.repositories.library_repo import LibraryRepository

_SUBS = [
    {"filename": "a.en.srt", "language": "eng", "title": None},
    {"filename": "a.chs.srt", "language": "chi", "title": "简体中文"},
]


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    reset_agent_session_store()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    async with get_database().session() as session:
        repo = LibraryRepository(session)
        movies = await repo.create(name="电影", kind="movie", root_paths=["/m"])
        shorts = await repo.create(name="动画短片", kind="movie", root_paths=["/s"])
        films = [
            (movies, MediaItem(kind="movie", tmdb_id=133701, title="钢铁之泪",
                               original_title="Tears of Steel", year=2012), _SUBS),
            (movies, MediaItem(kind="movie", tmdb_id=45745, title="寻龙记",
                               original_title="Sintel", year=2010), []),
            *(
                (shorts, MediaItem(kind="movie", tmdb_id=200 + i, title=f"Caminandes {i}",
                                   original_title=f"Caminandes {i}", year=2013), [])
                for i in range(3)
            ),
        ]  # fmt: skip
        session.add_all(item for _, item, _ in films)
        session.add(Member(username="family", password_hash="x", nickname="家人"))
        await session.flush()
        session.add_all(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=f"/x/{item.id}.mp4",
                size_bytes=300_000_000,
                source=FileSource.SCANNED,
                duration_seconds=600,
                resolution="1080p",
                video_codec="h264",
                bit_rate=4_000_000,
                external_subtitles=subs,
                state=FileState.IN_PLACE,
            )
            for library, item, subs in films
        )
        await session.commit()
    await demo_activity.seed_demo_data()
    yield get_database()
    await dispose_db()
    reset_agent_session_store()
    get_settings.cache_clear()


def _principal(device_id: int):
    return SimpleNamespace(name="admin", device=SimpleNamespace(id=device_id))


async def test_every_case_runs_through_the_real_runner(db) -> None:
    """每个预置问题 + 一句认不出的话，放进真实 AgentRunner 都能正常结束。"""
    questions = [q for _, q, _ in demo_agent._CASES] + ["你好", "帮我写一首诗", "Sintel 在吗"]
    for question in questions:
        runner = AgentRunner(demo_agent.router(), tools=demo_agent.tools())
        events = [
            e
            async for e in runner.start(
                AgentStartParams(input=question, history=[], system_prompt="x")
            )
        ]
        kinds = [e.type for e in events]
        assert kinds[-1] == "agent_done", (question, kinds, events[-1])
        results = [e.tool_result for e in events if e.type == "tool_result"]
        assert all(not r.is_error for r in results), (question, results)
        assert events[-1].result.text.strip()


async def test_subtitle_reply_uses_original_title_and_real_tracks(db) -> None:
    steps = await demo_agent.plan_reply("《钢铁之泪》有中文字幕吗？")
    text = "".join(s.text for s in steps)
    assert "钢铁之泪（Tears of Steel）" in text and "简体中文" in text
    # 「寻龙记」与商业片《寻龙诀》只差一字：原名在前
    steps = await demo_agent.plan_reply("Sintel 有字幕吗")
    assert "Sintel（寻龙记）" in "".join(s.text for s in steps)


async def test_device_sessions_are_isolated_complete_and_idempotent(db) -> None:
    alice, bob = _principal(1), _principal(2)
    await demo_agent.ensure_device_sessions(alice)
    await demo_agent.ensure_device_sessions(alice)  # 再打开一次列表：不翻倍
    await demo_agent.ensure_device_sessions(bob)

    async with db.session() as session:
        total = (await session.execute(select(func.count(AgentSession.id)))).scalar_one()
    assert total == 2 * len(demo_agent._CASES)

    alice_ids = set(demo_agent.visible_ids(alice))
    assert alice_ids.isdisjoint(demo_agent.visible_ids(bob))
    assert all(not demo_agent.is_visible(sid, bob) for sid in alice_ids)

    demo_agent.claim("visitor-session", bob)
    assert demo_agent.is_visible("visitor-session", bob)
    assert not demo_agent.is_visible("visitor-session", alice)

    store = get_agent_session_store()
    for sid in alice_ids:
        _, entries = store.read(sid)
        messages = [e.message for e in entries]
        assert messages[0].role == "user"
        assert messages[-1].role == "assistant" and not messages[-1].tool_calls
        calls = {c.id: c for m in messages for c in (m.tool_calls or [])}
        answered = {m.tool_call_id for m in messages if m.role == "tool"}
        assert set(calls) == answered, "每个工具调用都要有回执"
        for call in calls.values():
            if call.name == MEDIA_CARDS_TOOL:
                validate_items(call.arguments["component"], call.arguments["items"])
        # 续聊从这份转录重建历史，必须能原样喂回模型
        assert store.build_history(sid)
        json.dumps([m.model_dump() for m in messages], ensure_ascii=False)
