"""Concurrent admission (queue item: preserved branch `fix/concurrent-admission`, audited 2026-10-11): capacity must be
reserved from the moment a task is dispatched, not from the moment its worker process exists, and two tasks that own
overlapping files must not start in the same tick."""
import asyncio

from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.service import Service


def _service(tmp_path, ceiling=2):
    cfg = RuntimeConfig(data_dir=tmp_path / "data", workspaces_dir=tmp_path / "work", token_file=tmp_path / "token")
    cfg.scheduler.ceiling = ceiling
    cfg.scheduler.host_reserve_mb = 0
    cfg.scheduler.mem_per_worker_mb = 1
    cfg.scheduler.expected_cost_per_worker_hour_usd = 1
    return Service(cfg)


def _claiming_run_task(service, gate):
    """The admission-relevant behaviour of the real run_task: the task is marked RUNNING at once, then the workspace is
    prepared (a git clone, seconds) before `launcher.running` has an entry; here that stays pending until the gate opens."""
    async def run_task(task_id):
        service.store.set_task_status(task_id, "RUNNING")
        try:
            await gate.wait()
            service.store.set_task_status(task_id, "DONE")
        finally:
            service.scheduler._tasks.pop(task_id, None)          # the real run_task releases its slot in a finally
    service.scheduler.run_task = run_task


def test_slots_are_reserved_from_dispatch_not_from_worker_start(tmp_path):
    """Reproduced on main before the fix: with the ceiling at 2 and four independent READY tasks, a second tick ran while the
    first two were still cloning (launcher.running empty), saw two free slots again, and put four tasks in flight."""
    s = _service(tmp_path, ceiling=2)
    for tid, area in (("a", "a/x.py"), ("b", "b/x.py"), ("c", "c/x.py"), ("d", "d/x.py")):
        s.store.save_task(tid, "g", "desktop_agent", "READY", {"owned_area": [area], "dependencies": []}, tid)
    gate = asyncio.Event(); _claiming_run_task(s, gate)

    async def scenario():
        first = await s.scheduler.tick()
        assert first["launched"] == ["a", "b"]
        await asyncio.sleep(0)                                   # the launches have started; no worker process exists yet
        assert not s.launcher.running
        assert s.scheduler.slots()["running"] == 2 and s.scheduler.slots()["free"] == 0
        second = await s.scheduler.tick()
        assert second["launched"] == [], "a second tick admitted work beyond the ceiling while workspaces were still being prepared"
        assert len(s.scheduler._tasks) == 2
        gate.set()
        await asyncio.gather(*list(s.scheduler._tasks.values()))
        third = await s.scheduler.tick()                         # slots free again once the first two have finished
        assert third["launched"] == ["c", "d"]
        gate.set(); await asyncio.gather(*list(s.scheduler._tasks.values()))
    asyncio.run(scenario())


def test_overlapping_tasks_of_one_project_do_not_start_in_the_same_tick(tmp_path):
    """Reproduced on main before the fix: eligible() only sees tasks already RUNNING in the store, so two READY tasks owning the
    same file were both launched by one tick (the older edit-conflict race)."""
    s = _service(tmp_path, ceiling=3)
    for tid, area in (("a", "panel/index.html"), ("b", "panel/index.html"), ("c", "desktop_agent/control/router.py")):
        s.store.save_task(tid, "g", "desktop_agent", "READY", {"owned_area": [area], "dependencies": []}, tid)
    gate = asyncio.Event(); _claiming_run_task(s, gate)

    async def scenario():
        first = await s.scheduler.tick()
        assert first["launched"] == ["a", "c"], "b owns the same file as a and must wait"
        gate.set(); await asyncio.gather(*list(s.scheduler._tasks.values()))
        assert (await s.scheduler.tick())["launched"] == ["b"]   # once a is finished, b is free to start
        gate.set(); await asyncio.gather(*list(s.scheduler._tasks.values()))
    asyncio.run(scenario())


def test_other_projects_with_the_same_area_do_not_block_each_other(tmp_path):
    s = _service(tmp_path, ceiling=3)
    for tid, proj in (("a", "desktop_agent"), ("b", "caoscare")):
        s.store.save_task(tid, "g", proj, "READY", {"owned_area": ["README.md"], "dependencies": []}, tid)
    gate = asyncio.Event(); _claiming_run_task(s, gate)

    async def scenario():
        assert (await s.scheduler.tick())["launched"] == ["a", "b"]
        gate.set(); await asyncio.gather(*list(s.scheduler._tasks.values()))
    asyncio.run(scenario())
