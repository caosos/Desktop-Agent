"""Receipt event bridge, transport only (owner instruction da-de4a88e609).

Each meaningful coordinator receipt read back from the owner issue (ACK / WORKING / DONE / BLOCKED=NEED) is mirrored once as a
top-level comment on one persistent draft PR (`intake.bridge_pr`), so the existing GitHub PR event watcher can wake the Work
supervision conversation. The PR is not a coordinator, queue, watchdog or source of truth: Issue #3, docs/BUILD_CHECKLIST.md and
PROJECT_STATE stay authoritative. One PR comment per source comment id (kv `bridge:<id>`, written after a successful post), so
polling replays, restarts and repeated states mirror nothing twice. With no PR configured nothing is posted."""
from __future__ import annotations

from .checks_receipt import parse_receipt

LABEL = {"ACKNOWLEDGED": "ACK", "WORKING": "WORKING", "DONE": "DONE", "BLOCKED": "NEED"}
RANK = {"ACK": 0, "WORKING": 1, "NEED": 2, "DONE": 3}


def render(label: str, items: list[dict], text: str, url: str, loaded_head: str, created: str) -> str:
    f = parse_receipt(text)
    ids = ", ".join(i["item_id"] for i in items)
    g = lambda k: f.get(k) or "not stated"
    return "\n".join([
        f"DESKTOP AGENT RECEIPT: {label}",
        f"task: {f.get('task') or ids} ({ids})",
        f"source receipt: {url} at {created}",
        f"evidence: {g('evidence')}",
        f"eta: {g('eta')}",
        f"next_check_at: {g('next_check_at')}",
        f"blocker: {g('blocker')}",
        f"tested/loaded: control plane loaded revision {loaded_head or 'UNKNOWN'}; tested state as stated in the source receipt",
        "host observation: UNKNOWN (GitHub activity is not host-liveness proof)",
    ])


class Bridge:
    def __init__(self, store, post, loaded_head: str = ""):
        """post(path, fields) -> awaitable that creates a GitHub comment (the intake's gh_api in POST mode)."""
        self.store, self.post, self.loaded_head = store, post, loaded_head

    async def mirror(self, src, comment: dict, recorded: list[tuple[dict, str]]) -> dict | None:
        """`recorded` = (item, status) pairs this comment just advanced. At most one PR comment per source comment id."""
        pr = getattr(src, "bridge_pr", None)
        if not pr or not recorded:
            return None
        key = f"bridge:{comment.get('id')}"
        if self.store.get_kv(key):
            return None
        label = max((LABEL[s] for _, s in recorded), key=RANK.get)
        body = render(label, [i for i, _ in recorded], comment.get("body") or "", comment.get("html_url", ""), self.loaded_head, comment.get("created_at", ""))
        res = await self.post(f"repos/{src.repo}/issues/{pr}/comments", {"body": body})
        url = res.get("html_url") if isinstance(res, dict) else None
        self.store.set_kv(key, {"pr": pr, "label": label, "url": url})        # recorded only after the post succeeded
        return {"label": label, "url": url}
