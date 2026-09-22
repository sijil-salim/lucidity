#!/usr/bin/env python3
"""Event-driven enrollment worker (runs on the Ansible controller).

Flow: EC2 "instance running" event (any account) -> central bus -> SQS queue -> THIS worker ->
      ansible-playbook playbooks/enroll.yml -l <instance-id>

* Up to 10 events are batched into ONE Ansible run (autoscaling can start dozens of VMs at once).
* A VM that is not in the inventory yet (new account / not visible yet) triggers ONE inventory refresh + retry.
  Still unknown afterwards => it is opted out (Monitoring=disabled), terminated, or not ours: message dropped.
* Failure (typically the VM has not registered with SSM yet) => message is NOT deleted; SQS redelivers it after
  ~2 minutes; after 10 attempts it lands in the dead-letter queue, which raises an alarm.
* Enrollment is idempotent, so retries and duplicate events are harmless.

Env: QUEUE_URL (required unless --event-file), USE_ORG (non-empty => `generate_inventory.py --from-org`),
     AWS_CONFIG_FILE (profiles written by generate_inventory.py), AWS_DEFAULT_REGION.
Test without AWS:  scripts/enroll_worker.py --event-file tests/sample-ec2-running.json --dry-run
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
UNMATCHED = re.compile(r"Could not match supplied host pattern, ignoring: (\S+)")
RETRY_DELAY = 120          # seconds before a failed enrollment is retried


def log(msg):
    print(f"[enroll-worker] {msg}", flush=True)


def parse_event(body):
    """Return {'instance','account','region'} for an EC2 'running' event, else None."""
    try:
        ev = json.loads(body)
    except (TypeError, ValueError):
        return None
    detail = ev.get("detail") or {}
    iid = detail.get("instance-id")
    if ev.get("source") != "aws.ec2" or detail.get("state") != "running" or not iid:
        return None
    return {"instance": iid, "account": ev.get("account"), "region": ev.get("region")}


def run_enroll(ids, dry=False):
    cmd = ["ansible-playbook", "playbooks/enroll.yml", "-l", ",".join(ids)]
    log("$ " + " ".join(cmd))
    if dry:
        return 0, ""
    r = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
    sys.stdout.write(r.stdout[-3000:])
    sys.stderr.write(r.stderr[-1500:])
    return r.returncode, r.stdout + r.stderr


def refresh_inventory(use_org, dry=False):
    cmd = [sys.executable, "inventory/generate_inventory.py"] + (["--from-org"] if use_org else [])
    log("$ " + " ".join(cmd))
    if not dry:
        subprocess.run(cmd, cwd=REPO, check=False)


def _settle(ids, rc, dry):
    """Result for hosts that WERE in the inventory. If a batch failed, run them one by one to isolate the culprit."""
    if not ids:
        return {}
    if rc == 0:
        return {i: "ok" for i in ids}
    if len(ids) == 1:
        return {ids[0]: "retry"}
    out = {}
    for i in ids:
        r, _ = run_enroll([i], dry)
        out[i] = "ok" if r == 0 else "retry"
    return out


def enroll_batch(ids, dry=False, use_org=False):
    """Return {instance-id: 'ok' | 'skipped' | 'retry'}."""
    ids = sorted(set(ids))
    rc, out = run_enroll(ids, dry)
    unmatched = sorted(set(UNMATCHED.findall(out)) & set(ids))
    status = _settle([i for i in ids if i not in unmatched], rc, dry)
    if unmatched:                                   # not in inventory: refresh once and try again
        refresh_inventory(use_org, dry)
        rc2, out2 = run_enroll(unmatched, dry)
        gone = sorted(set(UNMATCHED.findall(out2)) & set(unmatched))
        for i in gone:
            log(f"{i}: not in inventory after refresh (opted out, terminated or unknown) - skipping")
            status[i] = "skipped"
        status.update(_settle([i for i in unmatched if i not in gone], rc2, dry))
    return status


def handle_messages(messages, delete_fn, retry_fn, dry=False, use_org=False):
    """messages: [{'Body','ReceiptHandle'}]. Calls delete_fn(handle) / retry_fn(handle)."""
    by_instance = {}
    for m in messages:
        parsed = parse_event(m["Body"])
        if parsed is None:
            delete_fn(m["ReceiptHandle"])          # not an event for us: drop it
            continue
        by_instance.setdefault(parsed["instance"], []).append(m["ReceiptHandle"])
    if not by_instance:
        return {}
    status = enroll_batch(list(by_instance), dry, use_org)
    for iid, handles in by_instance.items():
        for h in handles:
            (retry_fn if status.get(iid) == "retry" else delete_fn)(h)
        log(f"{iid}: {status.get(iid)}")
    return status


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue-url", default=os.environ.get("QUEUE_URL"))
    ap.add_argument("--from-org", action="store_true", default=bool(os.environ.get("USE_ORG")))
    ap.add_argument("--event-file", help="process this EventBridge event JSON instead of polling SQS")
    ap.add_argument("--dry-run", action="store_true", help="print the ansible commands, run nothing")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args()

    if args.event_file:
        body = pathlib.Path(args.event_file).read_text()
        status = handle_messages([{"Body": body, "ReceiptHandle": "file"}], lambda h: None, lambda h: None,
                                 args.dry_run, args.from_org)
        log(f"result: {status or 'event ignored'}")
        return 0
    if not args.queue_url:
        sys.exit("QUEUE_URL not set (or pass --queue-url)")

    import boto3                       # imported late so --event-file works without boto3 configured
    sqs = boto3.client("sqs")
    log(f"polling {args.queue_url}")
    while True:
        resp = sqs.receive_message(QueueUrl=args.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20,
                                   VisibilityTimeout=900)
        msgs = resp.get("Messages", [])
        if msgs:
            handle_messages(
                msgs,
                lambda h: sqs.delete_message(QueueUrl=args.queue_url, ReceiptHandle=h),
                lambda h: sqs.change_message_visibility(QueueUrl=args.queue_url, ReceiptHandle=h,
                                                        VisibilityTimeout=RETRY_DELAY),
                args.dry_run, args.from_org)
        if args.once:
            return 0
        if not msgs:
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
