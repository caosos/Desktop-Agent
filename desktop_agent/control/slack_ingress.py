"""Slack ingress decision logic (owner task DA-SLACK-INGRESS-001, da-9e6b71d929). Transport-independent: a Socket Mode client
(added once SLACK_APP_TOKEN / SLACK_BOT_TOKEN exist) feeds events to `SlackIngress.handle`.

Only genuine messages typed by Michael in the one channel are accepted; anything a bot, app, automation or this listener produced
is ignored even when Slack shows it under Michael's name (those events carry bot_id / app_id / a subtype / bot_profile). One
event_ts is acknowledged at most once (kv key written BEFORE the post, so a crash or Slack redelivery cannot double-ACK). A routed
message becomes a direction through the project's existing Shared Inbox path (`intake.send_direction`), never a new queue or
coordinator; every handled event leaves a receipt with its provenance. No model is called."""
from __future__ import annotations

import re
import time
from datetime import datetime

from .events import Actor
from .receipts import VERIFIED, write_receipt

ACK_MARK = "DOT ARYA ACK"                                 # prefix of every ACK this listener posts; a message starting with it is never accepted
ROUTES = {"DESKTOP AGENT": ["desktop_agent"], "CAOSCARE": ["caoscare"], "DEAL SNIFFER": ["michael_business_os"],
          "DOT ARIA": [], "ALL PROJECTS": ["desktop_agent", "caoscare", "michael_business_os"]}
# Deterministic human-facing aliases.  An alias is accepted only as a leading label followed by a colon or dash, so a sentence
# containing the ordinary word "care" cannot accidentally dispatch CAOSCare work.  Every alias resolves to one canonical label;
# the canonical label then resolves through the existing project descriptor and Shared Inbox transport.
ALIASES = {"DESKTOP AGENT": "DESKTOP AGENT", "ASK ARIA": "DESKTOP AGENT",
           "DEAL SNIFFER": "DEAL SNIFFER", "BUSINESS OS": "DEAL SNIFFER", "MICHAEL BUSINESS OS": "DEAL SNIFFER",
           "CAOSCARE": "CAOSCARE", "CARE APP": "CAOSCARE", "CARE": "CAOSCARE",
           "DOT ARIA": "DOT ARIA", "ALL PROJECTS": "ALL PROJECTS"}
_ROUTE_RE = re.compile(r"^\s*(" + "|".join(sorted(map(re.escape, ALIASES), key=len, reverse=True)) + r")\s*(?::|[-\u2013\u2014])\s*", re.I)
_VISIBLE_RE = re.compile(r"\b(can (anyone|anybody|you|someone) (see|read)|does (anyone|anybody) see|is (anyone|anybody) (seeing|reading)|do you see this|(anyone|anybody|someone) (listening|there|here|home|awake))\b", re.I)
_BOT_SUBTYPES = {"bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave", "file_share_bot"}


def classify(event: dict, channel: str, owner: str) -> tuple[bool, str]:
    """(accept, reason). Accept only a plain human message by `owner` in `channel` (thread replies included)."""
    if event.get("type") != "message":
        return False, "not a message event"
    if event.get("channel") != channel:
        return False, "other channel"
    if event.get("bot_id") or event.get("app_id") or event.get("bot_profile") or event.get("subtype") in _BOT_SUBTYPES:
        return False, "produced by a bot, app, automation or edit (even if shown under the owner's name)"
    if event.get("subtype") not in (None, "thread_broadcast"):
        return False, f"subtype {event.get('subtype')}"
    if event.get("user") != owner:
        return False, "not the owner"
    text = (event.get("text") or "").strip()
    if not text or text.upper().startswith(ACK_MARK):
        return False, "empty or this listener's own text"
    return True, "genuine owner message"


def route_for(text: str) -> tuple[str | None, list[str]]:
    m = _ROUTE_RE.match(text)
    if not m:
        return None, []
    label = ALIASES[m.group(1).upper()]
    return label, list(ROUTES[label])


def ack_text(label: str | None, received_at: float, visibility: bool, projects: list[str]) -> str:
    when = datetime.fromtimestamp(received_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    if visibility:
        return f"{ACK_MARK}: yes, I can see this message (received {when}). Nothing else was started."
    if label is None:
        return f"{ACK_MARK}: received {when}. No routing label (ASK ARIA:, DEAL SNIFFER:, CARE APP:, DOT ARIA:, ALL PROJECTS:), so I recorded it only. Add a label to route it."
    if not projects:
        return f"{ACK_MARK}: received {when}. Routed to {label}; recorded as a receipt for the supervisor, no project work started."
    return f"{ACK_MARK}: received {when}. Routed to {label} ({', '.join(projects)}) as a direction through the project's existing Shared Inbox. Next: its coordinator acknowledges on GitHub."


def _chicago(ts: float) -> str:
    from .checks_receipt import chicago
    return chicago(ts)


class SlackIngress:
    def __init__(self, store, intake, post, channel: str, owner: str, clock=time.time, mirror=None):
        """post(channel, thread_ts, text) -> awaitable; intake.send_direction is the only routing path."""
        self.store, self.intake, self.post, self.channel, self.owner, self.clock, self.mirror = store, intake, post, channel, owner, clock, mirror

    async def handle(self, event: dict) -> dict:
        ok, why = classify(event, self.channel, self.owner)
        if not ok:
            return {"handled": False, "ignored": why}
        ts = str(event.get("ts"))
        key = f"slack:{self.channel}:{ts}"
        if self.store.get_kv(key):
            return {"handled": False, "ignored": "already acknowledged (duplicate delivery)"}
        self.store.set_kv(key, {"at": self.clock()})                                   # recorded BEFORE posting: no double ACK on redelivery or crash
        text = event["text"].strip()
        label, projects = route_for(text)
        visibility = label is None and bool(_VISIBLE_RE.search(text))
        sent, errors = [], []
        for p in projects:
            try:
                item = await self.intake.send_direction(p, text, "slack")
                sent.append({"project": p, "item_id": item["item_id"], "url": item.get("url", ""), "status": item.get("status", "")})
            except Exception as exc:                                                   # a project without a coordinator never stops the others
                errors.append(f"{p}: {str(exc)[:80]}")
        reply = ack_text(label, self.clock(), visibility, [p for p in projects if not any(e.startswith(p + ":") for e in errors)])
        if errors:
            reply += " Not delivered: " + "; ".join(errors) + "."
        await self.post(self.channel, event.get("thread_ts") or ts, reply)
        item_ids = [d["item_id"] for d in sent]
        rec = write_receipt(self.store, subject_type="slack", subject_id=ts, claim=f"owner Slack message {ts} acknowledged; label {label or 'none'}; routed {len(sent)} direction(s)",
                            actor=Actor.CONTROL.value, source="slack_ingress", result_label=VERIFIED,
                            evidence=[f"channel={self.channel}", f"ts={ts}", f"user={self.owner}",
                                      *[f"route={d['project']} item={d['item_id']} destination={d['url'] or 'recorded locally'}" for d in sent], *errors],
                            after_state={"label": label, "routes": sent, "items": item_ids, "errors": errors, "visibility_answer": visibility})
        if self.mirror:                                                               # one receipt on each routed project's existing bridge
            mirror_errors = []
            for project in projects or ["desktop_agent"]:                            # preserve the working Desktop route for unlabelled/visibility ACKs
                project_sent = [d for d in sent if d["project"] == project]
                project_errors = [e for e in errors if e.startswith(project + ":")]
                try:
                    await self.mirror(project, self._receipt_text(ts, label, project_sent, project_errors, rec["receipt_id"], project))
                except Exception as exc:                                              # a failed mirror never loses the ACK or the project direction
                    mirror_errors.append(f"{project}: {str(exc)[:100]}")
            if mirror_errors:
                self.store.set_kv(key, {"at": self.clock(), "mirror_errors": mirror_errors})
        return {"handled": True, "label": label, "items": item_ids, "routes": sent, "errors": errors, "receipt": rec["receipt_id"]}

    def _receipt_text(self, ts: str, label: str | None, sent: list[dict], errors: list[str], receipt_id: str, project: str) -> str:
        try:
            typed = _chicago(float(ts))
        except ValueError:
            typed = ts
        destinations = "; ".join(f"{d['project']}={d['url'] or d['item_id']} ({d['status'] or 'recorded'})" for d in sent) or "none"
        return "\n".join([
            "DESKTOP AGENT RECEIPT: ACK",
            f"project: {project}", "task: DA-SLACK-THREE-PROJECT-ROUTING", "responsible: existing project coordinator through Shared Inbox",
            "stage: owner Slack message acknowledged once and routed to the named project's existing supervisor transport",
            f"evidence: slack channel {self.channel} ts {ts} (typed {typed}); control-plane receipt {receipt_id}; routing label {label or 'none'}; destinations {destinations}",
            f"acknowledged_at: {_chicago(self.clock())}", f"blocker: {'; '.join(errors) or 'NONE'}",
            "host observation: UNKNOWN (this receipt is GitHub transport, not host-liveness proof)"])
