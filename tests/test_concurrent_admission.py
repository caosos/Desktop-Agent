"""Reservations must count immediately and must respect edit ownership."""
import asyncio

from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.service import Service


def test_tick_reserves_only_independent_work_and_holds_slots(tmp_path):
    cfg = RuntimeConfig(data_dir=tmp_path / "data", workspaces_dir=tmp_path / "work",
                        token_file=tmp_path / "token")
    cfg.scheduler.ceiling = 2
    cfg.scheduler.host_reserve_mb = 0
    cfg.scheduler.mem_per_worker_mb = 1
    cfg.scheduler.expected_cost_per_worker_hour_usd = 1
    service = Service(cfg)
    for task_id, area in (("a", "panel/index.html"), ("b", "panel/index.html"),
                          ("c", "desktop_agent/control/router.py")):
        service.store.save_task(task_id, "g", "desktop_agent", "READY",
                                {"owned_area": [area], "dependencies": []}, task_id)

    gate = asyncio.Event()

    async def pending(task_id):
        await gate.wait()

    service.scheduler.run_task = pending

    async def scenario():
        first = await service.scheduler.tick()
        assert first["launched"] == ["a", "c"]
        # The launch coroutines have not started a worker process yet.
        assert not service.launcher.running
        assert service.scheduler.slots()["running"] == 2
        assert service.scheduler.slots()["free"] == 0
        assert (await service.scheduler.tick())["launched"] == []
        gate.set()
        await asyncio.gather(*service.scheduler._tasks.values())

    asyncio.run(scenario())
