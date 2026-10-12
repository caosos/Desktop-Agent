"""Michael's private personal memory for Ask Aria (owner directive da-c6d1e57152, privacy rules of da-970556bfe0).

Runtime-only store outside every repository (`RuntimeConfig.private_dir`, 0700 dir, 0600 files), separate from project
state and `aria:history`, never mounted into a worker, never written to logs, events, receipts, GitHub, Slack, telemetry,
error messages, API status or worker contracts. Memories reach the model only as private system context selected per
turn by relevance (`retrieval.py`); durable facts are captured after each turn without a model (`capture.py`).
"""
from .service import MemoryService  # noqa: E402,F401
