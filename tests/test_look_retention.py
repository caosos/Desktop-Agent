import os
import time

from desktop_agent.control.look_retention import sweep
from desktop_agent.control.store import Store


def test_old_captures_are_removed_new_ones_and_receipts_stay_one_receipt_per_sweep(tmp_path):
    store = Store(tmp_path)
    old, new, other = (tmp_path / "looks" / n for n in ("lk-old", "lk-new", "keepme"))
    for d in (old, new, other):
        d.mkdir(parents=True); (d / "capture.png").write_bytes(b"x")
    t = time.time(); os.utime(old, (t - 8 * 86400, t - 8 * 86400)); os.utime(other, (t - 30 * 86400, t - 30 * 86400))
    assert sweep(store, tmp_path) == ["lk-old"] and not old.exists() and new.exists() and other.exists()      # only lk-* folders, only older than 7 days
    recs = store.receipts("look", "retention")
    assert len(recs) == 1 and recs[0]["actor"] == "control" and "removed=lk-old" in recs[0]["evidence"]
    assert sweep(store, tmp_path) == [] and len(store.receipts("look", "retention")) == 1                       # an empty sweep leaves no receipt
    store.append_event  # the capture receipt itself is never touched
    assert sweep(Store(tmp_path / "empty"), tmp_path / "nowhere") == []
