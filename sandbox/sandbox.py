#!/usr/bin/env python3
"""Sandbox manager for the disk-monitoring solution.

Builds a small multi-account AWS lab so you can test ../aws-disk-monitoring for real:

  * "ops" side  : Ansible controller server + alert topic + OAM sink   (your current account)
  * "member" side: any number of accounts, each with an AnsibleOpsRole and test VMs

The desired state lives in sandbox.yaml.  Every command below edits that file and/or makes AWS
match it.  Add --dry-run to any command to print the AWS CLI calls without running them.

  ./sandbox.py init [--create-org]          one-time: controller, alert topic, dashboard
  ./sandbox.py apply                        make AWS match sandbox.yaml
  ./sandbox.py add-account NAME [--id ID]   new (real) AWS account, or adopt an existing one
  ./sandbox.py remove-account NAME [--close]
  ./sandbox.py add-vm ACCOUNT NAME [--os al2023|ubuntu|windows] [--count N] ...
  ./sandbox.py remove-vm ACCOUNT NAME
  ./sandbox.py list
  ./sandbox.py push-repo                    copy ../aws-disk-monitoring to the controller
  ./sandbox.py shell                        open a shell on the controller (Session Manager)
  ./sandbox.py destroy [--close-accounts]   delete everything this tool created

Needs: AWS CLI v2, PyYAML, credentials of the AWS Organizations MANAGEMENT account.
"""
import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import zipfile
import zlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent / "aws-disk-monitoring"
CFN_DIR = REPO / "cloudformation"
BUILD = ROOT / "build"
STATE_FILE = ROOT / ".sandbox-state.json"
ORG_REGION = "us-east-1"          # Organizations is a global service homed in us-east-1
DEFAULT_ROLE = "OrganizationAccountAccessRole"
HEADER = ("# Desired state of the sandbox. Edit by hand, or use the commands:\n"
          "#   ./sandbox.py add-account / remove-account / add-vm / remove-vm\n"
          "# (the commands rewrite this file; comments are not preserved - see README.md)\n")

OS_CATALOG = {
    "al2023": {"ssm": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
               "root": "/dev/xvda", "disk": 8, "param": "AmiAl2023"},
    "ubuntu": {"ssm": "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id",
               "root": "/dev/sda1", "disk": 8, "param": "AmiUbuntu"},
    "windows": {"ssm": "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base",
                "root": "/dev/sda1", "disk": 30, "param": "AmiWindows"},
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DRY = False
CONFIG_PATH = ROOT / "sandbox.yaml"


# ----------------------------------------------------------------------------- plumbing
def log(msg):
    print(msg, flush=True)


class Placeholder(dict):
    """Dry-run stand-in for stack outputs."""
    def __init__(self, label):
        super().__init__()
        self.label = label

    def __missing__(self, key):
        return f"<{self.label}.{key}>"


def _run(cmd, env=None, capture=False, check=True):
    shown = " ".join(shlex.quote(c) for c in cmd)
    if DRY:
        print(f"    [dry-run] {shown}")
        return ""
    r = subprocess.run(cmd, env={**os.environ, **(env or {})}, text=True, capture_output=capture)
    if r.returncode != 0:
        if check:
            sys.exit(f"\nCommand failed: {shown}\n{(r.stderr or '').strip()}")
        return None
    return r.stdout if capture else ""


def aws(*args, env=None, region=None, check=True):
    return _run(["aws", *args] + (["--region", region] if region else []), env, check=check)


def aws_json(*args, env=None, region=None, check=True, default=None):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    out = _run(cmd, env, capture=True, check=check)
    if DRY:
        return default if default is not None else {}
    if out is None:
        return None
    return json.loads(out) if out.strip() else {}


# ----------------------------------------------------------------------------- config / state
def logical_id(name):
    return "Vm" + "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", name) if p)


def load_cfg():
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg.setdefault("region", "us-east-1")
    cfg.setdefault("alert_email", "")
    cfg.setdefault("accounts", [])
    names = set()
    for a in cfg["accounts"]:
        if not NAME_RE.match(str(a.get("name", ""))):
            sys.exit(f"Bad account name {a.get('name')!r}: use lowercase letters, digits, dashes")
        if a["name"] in names:
            sys.exit(f"Duplicate account {a['name']}")
        names.add(a["name"])
        a.setdefault("vms", [])
        lids = set()
        for v in a["vms"]:
            if not NAME_RE.match(str(v.get("name", ""))):
                sys.exit(f"Bad VM name {v.get('name')!r} in {a['name']}")
            if v.get("os", "al2023") not in OS_CATALOG:
                sys.exit(f"VM {v['name']}: os must be one of {sorted(OS_CATALOG)}")
            lid = logical_id(v["name"])
            if lid in lids:
                sys.exit(f"VM names clash in {a['name']}: {v['name']}")
            lids.add(lid)
    return cfg


def save_cfg(cfg):
    CONFIG_PATH.write_text(HEADER + yaml.safe_dump(cfg, sort_keys=False))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    if DRY:
        return {"org_id": "o-dryrun0000", "home_account_id": "000000000000",
                "controller_role_arn": "arn:aws:iam::000000000000:role/AnsibleControllerRole",
                "sink_arn": "arn:aws:oam:us-east-1:000000000000:sink/dry-run",
                "topic_arn": "arn:aws:sns:us-east-1:000000000000:disk-alerts",
                "event_bus_arn": "arn:aws:events:us-east-1:000000000000:event-bus/disk-monitoring-events",
                "queue_url": "https://sqs.us-east-1.amazonaws.com/000000000000/disk-monitoring-enrollment",
                "controller_id": "i-0dryrun0000000000", "repo_bucket": "dry-run-repo-bucket",
                "accounts": {}}
    return {}


def save_state(st):
    if not DRY:
        STATE_FILE.write_text(json.dumps(st, indent=2))


def require_init(st):
    if not st.get("org_id"):
        sys.exit("Not initialised. Run: ./sandbox.py init")


# ----------------------------------------------------------------------------- CloudFormation helpers
def cfn_deploy(stack, template, env, params, region):
    cmd = ["cloudformation", "deploy", "--stack-name", stack, "--template-file", str(template),
           "--no-fail-on-empty-changeset", "--capabilities", "CAPABILITY_NAMED_IAM"]
    if params:
        cmd += ["--parameter-overrides", *[f"{k}={v}" for k, v in params.items()]]
    aws(*cmd, env=env, region=region)


def cfn_outputs(stack, env, region):
    if DRY:
        return Placeholder(stack)
    out = aws_json("cloudformation", "describe-stacks", "--stack-name", stack, env=env, region=region)
    return {o["OutputKey"]: o["OutputValue"] for o in out["Stacks"][0].get("Outputs", [])}


def cfn_delete(stack, env, region):
    aws("cloudformation", "delete-stack", "--stack-name", stack, env=env, region=region)
    aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", stack, env=env, region=region)


def write_template(name, doc):
    BUILD.mkdir(exist_ok=True)
    path = BUILD / f"{name}.json"
    path.write_text(json.dumps(doc, separators=(",", ":")))
    return path


def creds_for(account_id, role_name):
    if DRY:
        print(f"    [dry-run] aws sts assume-role --role-arn arn:aws:iam::{account_id}:role/{role_name}")
        return {}
    for attempt in range(12):     # a brand-new account needs a moment before the role works
        out = aws_json("sts", "assume-role", "--role-arn", f"arn:aws:iam::{account_id}:role/{role_name}",
                       "--role-session-name", "sandbox", check=False)
        if out:
            c = out["Credentials"]
            return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
                    "AWS_SESSION_TOKEN": c["SessionToken"]}
        log(f"  waiting for role in {account_id} ({attempt + 1}/12)...")
        time.sleep(15)
    sys.exit(f"Cannot assume {role_name} in {account_id}")


# ----------------------------------------------------------------------------- template builders
def network_resources():
    """Tiny public VPC. Instances get a public IP purely for outbound access; the security
    group has NO inbound rules - you reach VMs only through SSM."""
    vpc = {"Ref": "Vpc"}
    return {
        "Vpc": {"Type": "AWS::EC2::VPC", "Properties": {
            "CidrBlock": "10.0.0.0/16", "EnableDnsSupport": True, "EnableDnsHostnames": True,
            "Tags": [{"Key": "Name", "Value": "sandbox"}]}},
        "Igw": {"Type": "AWS::EC2::InternetGateway"},
        "IgwAttach": {"Type": "AWS::EC2::VPCGatewayAttachment",
                      "Properties": {"VpcId": vpc, "InternetGatewayId": {"Ref": "Igw"}}},
        "Subnet": {"Type": "AWS::EC2::Subnet", "Properties": {
            "VpcId": vpc, "CidrBlock": "10.0.1.0/24", "MapPublicIpOnLaunch": True,
            "AvailabilityZone": {"Fn::Select": [0, {"Fn::GetAZs": ""}]}}},
        "Rt": {"Type": "AWS::EC2::RouteTable", "Properties": {"VpcId": vpc}},
        "DefaultRoute": {"Type": "AWS::EC2::Route", "DependsOn": "IgwAttach", "Properties": {
            "RouteTableId": {"Ref": "Rt"}, "DestinationCidrBlock": "0.0.0.0/0", "GatewayId": {"Ref": "Igw"}}},
        "RtAssoc": {"Type": "AWS::EC2::SubnetRouteTableAssociation",
                    "Properties": {"SubnetId": {"Ref": "Subnet"}, "RouteTableId": {"Ref": "Rt"}}},
        "NoInboundSg": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "GroupDescription": "No inbound rules - access is via SSM only", "VpcId": vpc}},
    }


def ami_param(os_name):
    return {"Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>", "Default": OS_CATALOG[os_name]["ssm"]}


def instance(image_param, itype, profile, disk_gb, root_dev, tags, user_data=None):
    props = {
        "ImageId": {"Ref": image_param},
        "InstanceType": itype,
        "IamInstanceProfile": profile,
        "NetworkInterfaces": [{"DeviceIndex": "0", "AssociatePublicIpAddress": True,
                               "SubnetId": {"Ref": "Subnet"}, "GroupSet": [{"Ref": "NoInboundSg"}]}],
        "BlockDeviceMappings": [{"DeviceName": root_dev,
                                 "Ebs": {"VolumeSize": disk_gb, "VolumeType": "gp3", "Encrypted": True}}],
        "MetadataOptions": {"HttpTokens": "required"},      # IMDSv2 only
        "Tags": [{"Key": k, "Value": str(v)} for k, v in tags.items()],
    }
    if user_data:
        props["UserData"] = {"Fn::Base64": user_data}
    return {"Type": "AWS::EC2::Instance", "Properties": props}


def workload_template(acct):
    """Test VMs for one account. Generated from sandbox.yaml - add/remove a VM there and re-apply."""
    vms = acct.get("vms", [])
    used = sorted({v.get("os", "al2023") for v in vms})
    res = network_resources()
    res["VmRole"] = {"Type": "AWS::IAM::Role", "Properties": {
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
        # SSM registration + permission to publish metrics (the "instance role gap" from the README)
        "ManagedPolicyArns": ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                              "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"]}}
    res["VmProfile"] = {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [{"Ref": "VmRole"}]}}
    outputs = {}
    for v in vms:
        osn = v.get("os", "al2023")
        cat = OS_CATALOG[osn]
        tags = {"Name": v["name"], "Environment": v.get("env", "dev"),
                "Monitoring": "disabled" if v.get("monitoring") is False else "enabled"}
        tags.update(v.get("tags", {}))
        lid = logical_id(v["name"])
        res[lid] = instance(cat["param"], v.get("type", "t3.micro"), {"Ref": "VmProfile"},
                            v.get("disk_gb", cat["disk"]), cat["root"], tags)
        outputs[lid] = {"Value": {"Ref": lid}, "Description": f"instance id of {v['name']}"}
    doc = {"AWSTemplateFormatVersion": "2010-09-09",
           "Description": f"Sandbox test VMs for account '{acct['name']}' (generated by sandbox.py)",
           "Parameters": {OS_CATALOG[o]["param"]: ami_param(o) for o in used},
           "Resources": res}
    if outputs:
        doc["Outputs"] = outputs
    return doc


CONTROLLER_USERDATA = """#!/bin/bash
exec > /var/log/sandbox-bootstrap.log 2>&1
set -x
echo 'export AWS_DEFAULT_REGION=__REGION__' > /etc/profile.d/sandbox.sh
dnf install -y python3-pip git unzip
dnf install -y https://s3.amazonaws.com/session-manager-downloads/plugin/latest/linux_64bit/session-manager-plugin.rpm
pip3 install ansible boto3 botocore pyyaml
touch /var/lib/sandbox-controller-ready
"""


def controller_template(region):
    res = network_resources()
    res["RepoBucket"] = {"Type": "AWS::S3::Bucket", "Properties": {
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}}
    # extra permission for the controller role (created by ../aws-disk-monitoring/cloudformation/controller.yaml)
    res["RepoReadPolicy"] = {"Type": "AWS::IAM::ManagedPolicy", "Properties": {
        "Roles": ["AnsibleControllerRole"],
        "PolicyDocument": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "s3:GetObject", "Resource": {"Fn::Sub": "${RepoBucket.Arn}/*"}}]}}}
    res["Controller"] = instance("AmiAl2023", "t3.small", "AnsibleControllerProfile", 20, "/dev/xvda",
                                 {"Name": "sandbox-ansible-controller"},
                                 user_data=CONTROLLER_USERDATA.replace("__REGION__", region))
    res["Controller"]["DependsOn"] = "RepoReadPolicy"
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Sandbox Ansible controller host + repo hand-off bucket (generated by sandbox.py)",
            "Parameters": {"AmiAl2023": ami_param("al2023")},
            "Resources": res,
            "Outputs": {"ControllerId": {"Value": {"Ref": "Controller"}},
                        "RepoBucket": {"Value": {"Ref": "RepoBucket"}}}}


# ----------------------------------------------------------------------------- accounts
def plus_address(base, name):
    if not base or "@" not in base:
        sys.exit("Set email_base in sandbox.yaml (e.g. you@gmail.com) or pass --email")
    local, _, domain = base.partition("@")
    return f"{local}+sandbox-{name}@{domain}"


def resolved_id(st, acct):
    if acct.get("id") == "self":
        return st.get("home_account_id")
    if acct.get("id"):
        return str(acct["id"])
    return st.get("accounts", {}).get(acct["name"], {}).get("id")


def ensure_account_id(cfg, st, acct):
    name = acct["name"]
    known = resolved_id(st, acct)
    if known:
        return known
    email = acct.get("email") or plus_address(cfg.get("email_base"), name)
    found = aws_json("organizations", "list-accounts", region=ORG_REGION, default={"Accounts": []})
    for a in found.get("Accounts", []):
        if a["Email"].lower() == email.lower():
            st.setdefault("accounts", {})[name] = {"id": a["Id"], "managed": True}
            save_state(st)
            log(f"  found existing account {a['Id']} for {email}")
            return a["Id"]
    if DRY:
        fake = f"{zlib.crc32(name.encode()) % 10**12:012d}"
        print(f"    [dry-run] aws organizations create-account --email {email} --account-name sandbox-{name}")
        return fake
    log(f"  creating AWS account 'sandbox-{name}' ({email}) - takes 1-3 minutes")
    rid = aws_json("organizations", "create-account", "--email", email, "--account-name",
                   f"sandbox-{name}", region=ORG_REGION)["CreateAccountStatus"]["Id"]
    while True:
        s = aws_json("organizations", "describe-create-account-status", "--create-account-request-id", rid,
                     region=ORG_REGION)["CreateAccountStatus"]
        if s["State"] == "SUCCEEDED":
            break
        if s["State"] == "FAILED":
            sys.exit(f"Account creation failed: {s.get('FailureReason')}")
        time.sleep(10)
    st.setdefault("accounts", {})[name] = {"id": s["AccountId"], "managed": True}
    save_state(st)
    return s["AccountId"]


def account_env(st, acct, aid):
    if aid == st["home_account_id"]:
        return {}
    return creds_for(aid, acct.get("role_name", DEFAULT_ROLE))


def apply_account(cfg, st, acct):
    region = cfg["region"]
    log(f"\n== account '{acct['name']}'")
    aid = ensure_account_id(cfg, st, acct)
    is_home = aid == st["home_account_id"]
    env = account_env(st, acct, aid)
    log("  1/3 foundation: AnsibleOpsRole (+SSM default role)")
    cfn_deploy("disk-mon-member-global", CFN_DIR / "member-global.yaml", env,
               {"ControllerRoleArn": st["controller_role_arn"], "OrgId": st["org_id"]}, region)
    log("  2/3 foundation: transfer bucket, SSM setting, event forwarding" + ("" if is_home else ", metrics link to ops account"))
    cfn_deploy("disk-mon-member-regional", CFN_DIR / "member-regional.yaml", env,
               {"MonitoringSinkArn": "" if is_home else st["sink_arn"],
                "EventBusArn": st.get("event_bus_arn", "")}, region)
    log(f"  3/3 test VMs ({len(acct['vms'])}): {', '.join(v['name'] for v in acct['vms']) or 'none'}")
    tpl = write_template(f"workloads-{acct['name']}", workload_template(acct))
    cfn_deploy("sandbox-workloads", tpl, env, None, region)


def apply_all(cfg, st, only=None):
    require_init(st)
    for acct in cfg["accounts"]:
        if only and acct["name"] != only:
            continue
        apply_account(cfg, st, acct)
    log("\nDone. Next: ./sandbox.py push-repo   (then ./sandbox.py shell)")


def delete_alarms(env, region):
    out = aws_json("cloudwatch", "describe-alarms", "--alarm-name-prefix", "DiskUsage-", env=env,
                   region=region, check=False, default={"MetricAlarms": []}) or {}
    names = [a["AlarmName"] for a in out.get("MetricAlarms", [])]
    for i in range(0, len(names), 100):
        aws("cloudwatch", "delete-alarms", "--alarm-names", *names[i:i + 100], env=env, region=region)


def teardown_account(cfg, st, acct, close=False):
    region = cfg["region"]
    aid = resolved_id(st, acct)
    log(f"\n== removing account '{acct['name']}'")
    if not aid:
        log("  never created - nothing to delete")
        return
    env = account_env(st, acct, aid)
    log("  deleting Ansible-created alarms")
    delete_alarms(env, region)
    log("  deleting test VMs")
    cfn_delete("sandbox-workloads", env, region)
    log("  deleting foundation stacks")
    aws("s3", "rm", f"s3://ansible-ssm-{aid}-{region}", "--recursive", env=env, check=False)
    cfn_delete("disk-mon-member-regional", env, region)
    cfn_delete("disk-mon-member-global", env, region)
    if close and st.get("accounts", {}).get(acct["name"], {}).get("managed"):
        log("  closing the AWS account (it stays suspended ~90 days; closures are rate-limited)")
        aws("organizations", "close-account", "--account-id", aid, region=ORG_REGION)
    elif st.get("accounts", {}).get(acct["name"], {}).get("managed"):
        log("  account itself left in your Organization, empty (costs nothing). Use --close to close it.")
    st.get("accounts", {}).pop(acct["name"], None)
    save_state(st)


# ----------------------------------------------------------------------------- commands
def cmd_init(args):
    cfg, st = load_cfg(), load_state()
    region = cfg["region"]
    ident = aws_json("sts", "get-caller-identity", default={"Account": "000000000000"})
    org = aws_json("organizations", "describe-organization", region=ORG_REGION, check=False,
                   default={"Organization": {"Id": "o-dryrun0000", "MasterAccountId": ident["Account"]}})
    if not org:
        if not args.create_org:
            sys.exit("This account is not in an AWS Organization.\nCreate one (free): ./sandbox.py init --create-org")
        org = aws_json("organizations", "create-organization", "--feature-set", "ALL", region=ORG_REGION,
                       default={"Organization": {"Id": "o-dryrun0000", "MasterAccountId": ident["Account"]}})
    org = org["Organization"]
    if org["MasterAccountId"] != ident["Account"]:
        sys.exit("Run this with the Organizations MANAGEMENT account credentials "
                 "(it creates and manages the member accounts).")
    st.update({"org_id": org["Id"], "home_account_id": ident["Account"]})
    log(f"Organization {org['Id']}, home/ops account {ident['Account']}, region {region}")

    log("\n1/4 controller identity (IAM role that may assume AnsibleOpsRole)")
    cfn_deploy("ansible-controller", CFN_DIR / "controller.yaml", {}, None, region)
    st["controller_role_arn"] = cfn_outputs("ansible-controller", {}, region)["ControllerRoleArn"]

    log("\n2/4 alert topic, dashboard, metrics sink")
    cfn_deploy("disk-monitoring-hub", CFN_DIR / "monitoring-regional.yaml", {},
               {"OrgId": org["Id"], "AlertEmail": cfg.get("alert_email", "")}, region)
    out = cfn_outputs("disk-monitoring-hub", {}, region)
    st["sink_arn"], st["topic_arn"] = out["SinkArn"], out["AlertsTopicArn"]

    log("\n3/4 event-driven enrollment: central event bus + queue")
    cfn_deploy("disk-monitoring-enrollment", CFN_DIR / "enrollment-events.yaml", {},
               {"OrgId": org["Id"], "AlertsTopicArn": st["topic_arn"]}, region)
    out = cfn_outputs("disk-monitoring-enrollment", {}, region)
    st["event_bus_arn"], st["queue_url"] = out["EventBusArn"], out["QueueUrl"]

    log("\n4/4 Ansible controller server")
    tpl = write_template("controller-host", controller_template(region))
    cfn_deploy("sandbox-controller", tpl, {}, None, region)
    out = cfn_outputs("sandbox-controller", {}, region)
    st["controller_id"], st["repo_bucket"] = out["ControllerId"], out["RepoBucket"]
    st.setdefault("accounts", {})
    save_state(st)
    log("\nInitialised. Next: ./sandbox.py apply   (creates accounts/VMs from sandbox.yaml)")
    if cfg.get("alert_email"):
        log(f"Check {cfg['alert_email']} and confirm the SNS subscription, or you will get no alerts.")


def cmd_apply(args):
    apply_all(load_cfg(), load_state(), args.account)


def find_account(cfg, name):
    for a in cfg["accounts"]:
        if a["name"] == name:
            return a
    sys.exit(f"No account '{name}' in sandbox.yaml. Try ./sandbox.py list")


def cmd_add_account(args):
    cfg = load_cfg()
    if any(a["name"] == args.name for a in cfg["accounts"]):
        sys.exit(f"Account '{args.name}' already exists")
    if not NAME_RE.match(args.name):
        sys.exit("Use lowercase letters, digits, dashes")
    entry = {"name": args.name}
    if args.id:
        entry["id"] = args.id
    if args.email:
        entry["email"] = args.email
    if args.role_name:
        entry["role_name"] = args.role_name
    entry["vms"] = []
    cfg["accounts"].append(entry)
    save_cfg(cfg)
    log(f"Added '{args.name}' to sandbox.yaml" + (" (existing account)" if args.id else " (a NEW AWS account will be created)"))
    if not args.no_apply:
        apply_all(cfg, load_state(), args.name)


def cmd_remove_account(args):
    cfg, st = load_cfg(), load_state()
    acct = find_account(cfg, args.name)
    require_init(st)
    teardown_account(cfg, st, acct, close=args.close)
    cfg["accounts"] = [a for a in cfg["accounts"] if a["name"] != args.name]
    save_cfg(cfg)
    log(f"Removed '{args.name}' from sandbox.yaml")


def cmd_add_vm(args):
    cfg = load_cfg()
    acct = find_account(cfg, args.account)
    names = [args.name] if args.count == 1 else [f"{args.name}-{i}" for i in range(1, args.count + 1)]
    for n in names:
        if not NAME_RE.match(n):
            sys.exit(f"Bad VM name {n!r}")
        if any(v["name"] == n for v in acct["vms"]):
            sys.exit(f"VM '{n}' already exists in {args.account}")
    for n in names:
        vm = {"name": n, "os": args.os, "env": args.env}
        if args.type:
            vm["type"] = args.type
        if args.disk_gb:
            vm["disk_gb"] = args.disk_gb
        if args.no_monitoring:
            vm["monitoring"] = False
        acct["vms"].append(vm)
    save_cfg(cfg)
    log(f"Added {', '.join(names)} to '{args.account}'")
    if not args.no_apply:
        apply_all(cfg, load_state(), args.account)


def cmd_remove_vm(args):
    cfg = load_cfg()
    acct = find_account(cfg, args.account)
    before = len(acct["vms"])
    acct["vms"] = [v for v in acct["vms"] if v["name"] != args.name]
    if len(acct["vms"]) == before:
        sys.exit(f"No VM '{args.name}' in {args.account}")
    save_cfg(cfg)
    log(f"Removed '{args.name}' from '{args.account}'")
    if not args.no_apply:
        apply_all(cfg, load_state(), args.account)


def cmd_list(_args):
    cfg, st = load_cfg(), load_state()
    log(f"region: {cfg['region']}   org: {st.get('org_id', '(not initialised)')}   "
        f"controller: {st.get('controller_id', '-')}")
    for a in cfg["accounts"]:
        aid = resolved_id(st, a) or "(not created yet)"
        kind = "home/ops" if a.get("id") == "self" else ("existing" if a.get("id") else "tool-created")
        log(f"\n{a['name']}  [{aid}]  {kind}")
        for v in a["vms"]:
            mon = "monitoring OFF" if v.get("monitoring") is False else "monitored"
            log(f"   - {v['name']:<16} {v.get('os', 'al2023'):<8} env={v.get('env', 'dev'):<8} {mon}")
        if not a["vms"]:
            log("   (no VMs)")


def build_repo_zip(cfg, st):
    """Zip ../aws-disk-monitoring, swapping in accounts.yml / ops_account_id generated from the sandbox."""
    BUILD.mkdir(exist_ok=True)
    out = BUILD / "repo.zip"
    accounts = []
    for a in cfg["accounts"]:
        aid = resolved_id(st, a)
        if aid:
            accounts.append({"id": str(aid), "name": a["name"]})
    accounts_doc = {"ops_role_name": "AnsibleOpsRole", "credential_source": "Ec2InstanceMetadata",
                    "default_regions": [cfg["region"]], "exclude_account_ids": [], "accounts": accounts}
    skip = {".git", "__pycache__", "generated", "ansible_collections"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(REPO.rglob("*")):
            rel = p.relative_to(REPO)
            if p.is_dir() or skip & set(rel.parts):
                continue
            arc = f"aws-disk-monitoring/{rel.as_posix()}"
            if rel.as_posix() == "inventory/accounts.yml":
                z.writestr(arc, "# GENERATED by sandbox.py push-repo\n" + yaml.safe_dump(accounts_doc, sort_keys=False))
            elif rel.as_posix() == "playbooks/group_vars/all.yml":
                text = re.sub(r"(?m)^ops_account_id:.*$", f'ops_account_id: "{st["home_account_id"]}"', p.read_text())
                z.writestr(arc, text)
            else:
                z.write(p, arc)
    return out


def run_on_controller(st, region, commands):
    """Run `commands` on the controller as one shell script and FAIL LOUDLY if any line fails.
    AWS-RunShellScript runs multiple commands as one script with no error checking by default, so a
    failing step (e.g. a collection install) would otherwise be silently skipped over while later
    steps still run - `set -e` (prepended below) stops the script at the first failure, and we then
    check the invocation's real status and print its output, instead of only waiting for it to finish."""
    params = json.dumps({"commands": ["set -e", "set -o pipefail"] + commands})
    for _ in range(10):
        out = aws_json("ssm", "send-command", "--instance-ids", st["controller_id"], "--document-name",
                       "AWS-RunShellScript", "--parameters", params, region=region, check=False,
                       default={"Command": {"CommandId": "dry-run"}})
        if out:
            break
        log("  controller not registered with SSM yet (still booting?) - retrying in 30s")
        time.sleep(30)
    else:
        sys.exit("Controller never became reachable via SSM. Check the instance in the EC2 console.")
    command_id = out["Command"]["CommandId"]
    if DRY:
        return
    aws("ssm", "wait", "command-executed", "--command-id", command_id,
        "--instance-id", st["controller_id"], region=region, check=False)
    inv = aws_json("ssm", "get-command-invocation", "--command-id", command_id,
                   "--instance-id", st["controller_id"], region=region, check=False, default={})
    status = inv.get("Status", "Unknown")
    out_text, err_text = inv.get("StandardOutputContent", ""), inv.get("StandardErrorContent", "")
    if status != "Success":
        log(f"  FAILED on the controller (status: {status}). Last output:")
        for line in (out_text + "\n" + err_text).strip().splitlines()[-25:]:
            log(f"    {line}")
        sys.exit(f"Controller command failed (status: {status}) - see output above and fix before retrying.")
    if out_text.strip():
        log("  controller output:")
        for line in out_text.strip().splitlines()[-15:]:
            log(f"    {line}")


def cmd_push_repo(_args):
    cfg, st = load_cfg(), load_state()
    require_init(st)
    if not REPO.exists():
        sys.exit(f"Repo folder not found: {REPO}")
    region = cfg["region"]
    zpath = build_repo_zip(cfg, st)
    log(f"Uploading {zpath.name} and unpacking on the controller (/opt/aws-disk-monitoring)")
    aws("s3", "cp", str(zpath), f"s3://{st['repo_bucket']}/repo.zip", region=region)
    run_on_controller(st, region, [
        f"aws s3 cp s3://{st['repo_bucket']}/repo.zip /tmp/repo.zip --region {region}",
        "rm -rf /opt/aws-disk-monitoring && unzip -q -o /tmp/repo.zip -d /opt",
        "chown -R ec2-user:ec2-user /opt/aws-disk-monitoring",
        # Persist AWS_CONFIG_FILE for ec2-user's own interactive sessions (e.g. `sandbox.py shell`), so manual
        # ansible-playbook runs work without re-exporting it every time. The systemd services (worker, reconcile)
        # already get it independently via their own Environment= line - this is purely for the human running by hand.
        "grep -qxF 'export AWS_CONFIG_FILE=/opt/aws-disk-monitoring/inventory/aws_config' /home/ec2-user/.bashrc "
        "|| echo 'export AWS_CONFIG_FILE=/opt/aws-disk-monitoring/inventory/aws_config' >> /home/ec2-user/.bashrc",
        # Absolute -p path (not relative "./collections") so this can't land in the wrong place regardless of
        # which directory the command actually runs from. Then a hard check: if the collection truly isn't on
        # disk afterward, fail LOUDLY right here with an unambiguous message, instead of silently moving on.
        "sudo -u ec2-user env HOME=/home/ec2-user ansible-galaxy collection install "
        "-r /opt/aws-disk-monitoring/collections/requirements.yml "
        "-p /opt/aws-disk-monitoring/collections --force",
        "test -d /opt/aws-disk-monitoring/collections/ansible_collections/amazon/aws "
        "&& echo 'COLLECTION_INSTALL_OK' "
        "|| { echo 'COLLECTION_INSTALL_FAILED: amazon.aws is not at /opt/aws-disk-monitoring/collections after install'; exit 1; }",
        "sudo -u ec2-user env HOME=/home/ec2-user python3 /opt/aws-disk-monitoring/inventory/generate_inventory.py "
        "--accounts /opt/aws-disk-monitoring/inventory/accounts.yml "
        "--out /opt/aws-disk-monitoring/inventory/generated "
        "--aws-config-out /opt/aws-disk-monitoring/inventory/aws_config",
        # event-driven enrollment worker + hourly reconcile timer (safety net)
        f"printf 'QUEUE_URL=%s\\nAWS_DEFAULT_REGION=%s\\n' {shlex.quote(st['queue_url'])} {region} > /etc/disk-monitoring.env",
        "cp /opt/aws-disk-monitoring/scripts/systemd/* /etc/systemd/system/ && systemctl daemon-reload",
        "systemctl enable --now reconcile.timer && systemctl enable enroll-worker.service && systemctl restart enroll-worker.service",
    ])
    log("Enrollment worker started on the controller: new VMs are enrolled automatically within minutes of starting; an hourly reconcile timer is the safety net.")
    log("Done. Now: ./sandbox.py shell   then:  sudo su - ec2-user  &&  cd /opt/aws-disk-monitoring")
    log("Watch it work:  sudo journalctl -u enroll-worker -f")
    log("(see README.md 'On the controller' for the next commands)")


def cmd_shell(_args):
    cfg, st = load_cfg(), load_state()
    require_init(st)
    cmd = ["aws", "ssm", "start-session", "--target", st["controller_id"], "--region", cfg["region"]]
    if DRY:
        print("    [dry-run] " + " ".join(cmd))
        return
    os.execvp("aws", cmd)      # needs the session-manager-plugin on THIS machine


def cmd_destroy(args):
    cfg, st = load_cfg(), load_state()
    require_init(st)
    if not args.yes and input("Delete EVERYTHING this tool created? Type 'destroy': ").strip() != "destroy":
        sys.exit("Aborted.")
    region = cfg["region"]
    for a in [a for a in cfg["accounts"] if a.get("id") != "self"] + [a for a in cfg["accounts"] if a.get("id") == "self"]:
        teardown_account(cfg, st, a, close=args.close_accounts)
    log("\n== removing controller, alert topic, controller role")
    aws("s3", "rm", f"s3://{st['repo_bucket']}", "--recursive", region=region, check=False)
    cfn_delete("sandbox-controller", {}, region)
    cfn_delete("disk-monitoring-enrollment", {}, region)
    cfn_delete("disk-monitoring-hub", {}, region)
    cfn_delete("ansible-controller", {}, region)
    if not DRY and STATE_FILE.exists():
        STATE_FILE.unlink()
    log("Destroyed. (The AWS Organization itself is left in place.)")


# ----------------------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print AWS CLI calls, change nothing")
    p.add_argument("--config", help="path to sandbox.yaml (default: next to this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="one-time: controller, alert topic, dashboard")
    s.add_argument("--create-org", action="store_true", help="create an AWS Organization if there is none")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("apply", help="make AWS match sandbox.yaml")
    s.add_argument("--account", help="only this account")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("add-account", help="add an account (new, or existing with --id)")
    s.add_argument("name")
    s.add_argument("--id", help="12-digit id of an EXISTING account (default: create a new one)")
    s.add_argument("--email", help="email for a new account (default: <email_base>+sandbox-<name>)")
    s.add_argument("--role-name", help=f"admin role to assume (default {DEFAULT_ROLE})")
    s.add_argument("--no-apply", action="store_true", help="only edit sandbox.yaml")
    s.set_defaults(fn=cmd_add_account)

    s = sub.add_parser("remove-account", help="delete an account's VMs/stacks and drop it from sandbox.yaml")
    s.add_argument("name")
    s.add_argument("--close", action="store_true", help="also CLOSE the AWS account (rate-limited, ~90-day suspension)")
    s.set_defaults(fn=cmd_remove_account)

    s = sub.add_parser("add-vm", help="add one (or --count N) test VMs to an account")
    s.add_argument("account")
    s.add_argument("name")
    s.add_argument("--os", default="al2023", choices=sorted(OS_CATALOG))
    s.add_argument("--env", default="dev", help="value of the Environment tag")
    s.add_argument("--type", help="instance type (default t3.micro)")
    s.add_argument("--disk-gb", type=int, help="root disk size (small disks fill fast: good for demos)")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--no-monitoring", action="store_true", help="tag Monitoring=disabled (opt-out test)")
    s.add_argument("--no-apply", action="store_true")
    s.set_defaults(fn=cmd_add_vm)

    s = sub.add_parser("remove-vm", help="remove a test VM")
    s.add_argument("account")
    s.add_argument("name")
    s.add_argument("--no-apply", action="store_true")
    s.set_defaults(fn=cmd_remove_vm)

    sub.add_parser("list", help="show accounts and VMs").set_defaults(fn=cmd_list)
    sub.add_parser("push-repo", help="copy ../aws-disk-monitoring onto the controller").set_defaults(fn=cmd_push_repo)
    sub.add_parser("shell", help="shell on the controller via Session Manager").set_defaults(fn=cmd_shell)

    s = sub.add_parser("destroy", help="delete everything this tool created")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--close-accounts", action="store_true", help="also close tool-created accounts")
    s.set_defaults(fn=cmd_destroy)
    return p


def main():
    global DRY, CONFIG_PATH
    args = build_parser().parse_args()
    DRY = args.dry_run
    if args.config:
        CONFIG_PATH = pathlib.Path(args.config).resolve()
    if DRY:
        log("*** DRY RUN: nothing will be changed ***")
    args.fn(args)


if __name__ == "__main__":
    main()
