"""verify_live's comparison logic, on offline fixtures (no network)."""
import json
import shutil

from finrecon import verify_live as V


def test_identical_snapshots_have_no_differences():
    d = V.diff_snapshots(V.DEPLOYED, V.DEPLOYED)
    assert d and all(not any(v.values()) for v in d.values())


def test_added_changed_removed_objects_are_reported(tmp_path):
    copy = tmp_path / "snap"
    shutil.copytree(V.DEPLOYED, copy)
    p = copy / "stripe" / "payment_intents.json"
    objs = json.loads(p.read_text())
    removed = objs.pop()["id"]
    objs[0]["amount"] += 1
    objs.append({"id": "pi_new", "amount": 1})
    p.write_text(json.dumps(objs))
    d = V.diff_snapshots(V.DEPLOYED, copy)["stripe/payment_intents.json"]
    assert d["added"] == ["pi_new"] and d["removed"] == [removed] and d["changed"] == [objs[0]["id"]]


def test_volatile_fields_are_ignored(tmp_path):
    copy = tmp_path / "snap"
    shutil.copytree(V.DEPLOYED, copy)
    p = copy / "qbo" / "Invoice.json"
    objs = json.loads(p.read_text())
    objs[0]["SyncToken"] = "99"
    objs[0]["MetaData"]["LastUpdatedTime"] = "2030-01-01T00:00:00-08:00"
    p.write_text(json.dumps(objs))
    assert not any(V.diff_snapshots(V.DEPLOYED, copy)["qbo/Invoice.json"].values())


def test_snapshot_reconciles_offline(tmp_path):
    db, run_id, ev = V.run_offline(V.DEPLOYED, tmp_path)
    assert ev["scenarios_passed"] == ev["scenarios_total"] == 27
    assert V.results_of(db, run_id)
