import asyncio
from services import selfping
def test_selfping_calls_health_and_survives_errors(monkeypatch):
    calls = []
    def fake(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("boom")
        return 200
    monkeypatch.setattr(selfping, "_get", fake)
    async def run():
        t = asyncio.create_task(selfping.loop("https://x.onrender.com/", interval=0.01))
        await asyncio.sleep(0.15)
        t.cancel()
    asyncio.run(run())
    assert len(calls) >= 2 and calls[0] == "https://x.onrender.com/health"
