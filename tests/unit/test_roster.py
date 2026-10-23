import asyncio

from server.config import LimitsConfig
from server.roster import Roster
from tests.helpers import RecordingSocket, wait_for


def test_routes_and_idempotent_removal():
    async def scenario():
        roster = Roster()
        teacher, en, zh = RecordingSocket(), RecordingSocket(), RecordingSocket()
        t = await roster.add(teacher, "teacher")
        a = await roster.add(en, "student", "en")
        b = await roster.add(zh, "student", "zh")
        assert roster.active_langs() == {"en", "zh"}
        roster.send_teacher({"type": "teacher"})
        roster.send_language("en", {"type": "caption"})
        roster.broadcast({"type": "all"})
        await wait_for(teacher, "all")
        await wait_for(en, "all")
        await wait_for(zh, "all")
        assert [m["type"] for m in teacher.sent] == ["teacher", "all"]
        assert [m["type"] for m in en.sent] == ["caption", "all"]
        assert [m["type"] for m in zh.sent] == ["all"]
        await roster.remove(b)
        await roster.remove(b, close_code=1000)
        assert zh.closed_with is None
        assert roster.active_langs() == {"en"}
        await roster.retry(a)
        assert en.closed_with == 1013
        await roster.remove(t, close_code=1000)
        assert teacher.closed_with == 1000
        assert not roster.clients
    asyncio.run(scenario())


def test_teacher_replacement_registers_before_old_transport_closes():
    async def scenario():
        roster = Roster()
        class OldSocket(RecordingSocket):
            async def close(self, code=1000):
                assert roster.teacher().ws is replacement
                await super().close(code)
        old = OldSocket()
        await roster.add(old, "teacher")
        replacement = RecordingSocket()
        current = await roster.add(replacement, "teacher", initial={"type": "joined"})
        assert roster.teacher() is current
        assert old.closed_with == 4000
        await wait_for(replacement, "joined")
        await roster.aclose()
    asyncio.run(scenario())


def test_overflow_closes_only_slow_connection():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowSocket(RecordingSocket):
            async def send_json(self, data):
                entered.set()
                await release.wait()
                await super().send_json(data)
        roster = Roster(LimitsConfig(send_queue_max=2))
        slow, fast = SlowSocket(), RecordingSocket()
        await roster.add(slow, "student", "en")
        await roster.add(fast, "student", "en")
        roster.broadcast({"type": "caption", "seq": 0})
        await entered.wait()
        await wait_for(fast, "caption", seq=0)
        for seq in range(1, 4):
            roster.broadcast({"type": "caption", "seq": seq})
            await wait_for(fast, "caption", seq=seq)
        assert slow.closed_with == 1013
        assert fast.closed_with is None
        release.set()
        await roster.aclose()
    asyncio.run(scenario())
