#!/usr/bin/env python3
"""Report VMs that should be monitored but are not.

Per account/region (via the same AnsibleOpsRole Ansible uses):
  * running EC2 instances not tagged Monitoring=disabled
  * SSM PingStatus for each   -> "not reachable by SSM"
  * CWAgent disk metrics seen in the last 3h -> "agent not reporting"
Exit code 0 = full coverage, 2 = gaps.
"""
import argparse
import json
import sys

import boto3
import yaml

DISK_METRICS = ("disk_used_percent", "LogicalDisk % Free Space")


def session_for(account_id, role_name, region):
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
        RoleSessionName="ansible-coverage-report",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def expected_instances(ec2):
    ids = {}
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    for page in pages:
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                if tags.get("Monitoring") != "disabled":
                    ids[inst["InstanceId"]] = tags.get("Name", "")
    return ids


def ssm_online(ssm):
    online = set()
    for page in ssm.get_paginator("describe_instance_information").paginate():
        online |= {i["InstanceId"] for i in page["InstanceInformationList"] if i["PingStatus"] == "Online"}
    return online


def reporting(cw):
    seen = set()
    for metric in DISK_METRICS:
        pages = cw.get_paginator("list_metrics").paginate(
            Namespace="CWAgent", MetricName=metric, RecentlyActive="PT3H"
        )
        for page in pages:
            for m in page["Metrics"]:
                seen |= {d["Value"] for d in m["Dimensions"] if d["Name"] == "InstanceId"}
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="inventory/accounts.yml")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.accounts))

    gaps, total = [], 0
    for acct in cfg["accounts"]:
        for region in acct.get("regions", cfg["default_regions"]):
            s = session_for(acct["id"], cfg["ops_role_name"], region)
            want = expected_instances(s.client("ec2"))
            online, seen = ssm_online(s.client("ssm")), reporting(s.client("cloudwatch"))
            total += len(want)
            for iid, name in want.items():
                if iid not in online:
                    reason = "not reachable by SSM (agent/role/network)"
                elif iid not in seen:
                    reason = "SSM ok but CloudWatch agent not reporting -> run enroll.yml"
                else:
                    continue
                gaps.append({"account": acct["name"], "region": region, "instance": iid, "name": name, "reason": reason})

    if args.json:
        print(json.dumps({"total": total, "gaps": gaps}, indent=2))
    else:
        print(f"{total - len(gaps)}/{total} VMs monitored")
        for g in gaps:
            print(f"GAP {g['account']}/{g['region']} {g['instance']} ({g['name']}): {g['reason']}")
    return 2 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
