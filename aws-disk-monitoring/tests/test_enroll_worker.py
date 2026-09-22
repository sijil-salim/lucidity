"""Unit tests for scripts/enroll_worker.py (no AWS, no Ansible needed).  Run: python -m unittest discover tests"""
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import enroll_worker as w  # noqa: E402

SAMPLE = (pathlib.Path(__file__).parent / "sample-ec2-running.json").read_text()


def warn(*ids):
    return "\n".join(f"[WARNING]: Could not match supplied host pattern, ignoring: {i}" for i in ids)


class ParseEvent(unittest.TestCase):
    def test_running_event(self):
        self.assertEqual(w.parse_event(SAMPLE),
                         {"instance": "i-0abc1234567890def", "account": "222222222222", "region": "us-east-1"})

    def test_ignores_other_states_sources_and_garbage(self):
        ev = json.loads(SAMPLE)
        ev["detail"]["state"] = "stopped"
        self.assertIsNone(w.parse_event(json.dumps(ev)))
        ev = json.loads(SAMPLE)
        ev["source"] = "aws.s3"
        self.assertIsNone(w.parse_event(json.dumps(ev)))
        self.assertIsNone(w.parse_event("not json"))
        self.assertIsNone(w.parse_event(json.dumps({"detail": {}})))


class EnrollBatch(unittest.TestCase):
    @mock.patch.object(w, "refresh_inventory")
    @mock.patch.object(w, "run_enroll")
    def test_all_ok_single_ansible_run(self, run, refresh):
        run.return_value = (0, "")
        self.assertEqual(w.enroll_batch(["i-1", "i-2", "i-2"]), {"i-1": "ok", "i-2": "ok"})
        run.assert_called_once_with(["i-1", "i-2"], False)      # batched + de-duplicated
        refresh.assert_not_called()

    @mock.patch.object(w, "refresh_inventory")
    @mock.patch.object(w, "run_enroll")
    def test_unknown_vm_refreshes_inventory_then_succeeds(self, run, refresh):
        run.side_effect = [(0, warn("i-2")), (0, "")]            # first: i-2 not in inventory; after refresh: found
        self.assertEqual(w.enroll_batch(["i-1", "i-2"]), {"i-1": "ok", "i-2": "ok"})
        refresh.assert_called_once()

    @mock.patch.object(w, "refresh_inventory")
    @mock.patch.object(w, "run_enroll")
    def test_opted_out_vm_is_skipped_not_retried_forever(self, run, refresh):
        run.side_effect = [(1, warn("i-9")), (1, warn("i-9"))]   # never appears (Monitoring=disabled)
        self.assertEqual(w.enroll_batch(["i-9"]), {"i-9": "skipped"})

    @mock.patch.object(w, "refresh_inventory")
    @mock.patch.object(w, "run_enroll")
    def test_failing_vm_is_isolated_and_healthy_ones_still_succeed(self, run, refresh):
        def fake(ids, dry=False):
            return (4, "") if "i-bad" in ids else (0, "")        # i-bad unreachable (SSM not ready)
        run.side_effect = fake
        self.assertEqual(w.enroll_batch(["i-ok1", "i-bad", "i-ok2"]),
                         {"i-ok1": "ok", "i-bad": "retry", "i-ok2": "ok"})


class HandleMessages(unittest.TestCase):
    @mock.patch.object(w, "enroll_batch")
    def test_delete_vs_retry_and_garbage_dropped(self, batch):
        e = lambda i: json.dumps({"source": "aws.ec2", "account": "1", "region": "r",
                                  "detail": {"instance-id": i, "state": "running"}})
        batch.return_value = {"i-1": "ok", "i-2": "retry", "i-3": "skipped"}
        deleted, retried = [], []
        msgs = [{"Body": e("i-1"), "ReceiptHandle": "h1"}, {"Body": e("i-2"), "ReceiptHandle": "h2"},
                {"Body": e("i-3"), "ReceiptHandle": "h3"}, {"Body": "junk", "ReceiptHandle": "h4"}]
        w.handle_messages(msgs, deleted.append, retried.append)
        self.assertCountEqual(deleted, ["h1", "h3", "h4"])
        self.assertEqual(retried, ["h2"])


if __name__ == "__main__":
    unittest.main()
