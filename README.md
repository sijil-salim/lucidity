# Scalable Disk Monitoring Solution for Cloud Environments (AWS)

## Overview

This solution monitors disk utilization across every VM in every AWS account in a multi-account
organization, detecting low disk space early enough to prevent downtime. It is built on the company's
existing configuration management tool, Ansible, and incorporates AWS-native services only where they
provide a substantial benefit over what Ansible alone can do: AWS Systems Manager for secure, keyless
access to VMs; CloudWatch for metric collection, alerting, and cross-account visualization; and
CloudFormation StackSets for automatic, zero-touch onboarding of new accounts.

The design rests on a simple separation of concerns. A lightweight, AWS-managed agent on every VM
continuously measures disk usage and reports it, independently of Ansible. Ansible's role is limited to
*setup*: discovering VMs, installing and configuring that agent, and provisioning the alarms that watch
its data. This means the monitoring itself, the alerts and dashboards, keeps running even if the Ansible
controller is temporarily unavailable. Setup runs automatically the moment a new VM starts, driven by an
event pipeline rather than a fixed schedule, with a periodic reconciliation pass as a safety net for
anything the event path might miss.

A high-level architecture diagram is provided in [docs/architecture.md](docs/architecture.md), and a
plain-language walkthrough of the same flow is in [docs/architecture-simple.md](docs/architecture-simple.md).
A full glossary of every component, IAM role, and AWS resource this solution creates is in [docs/components.md](docs/components.md).

---

## 1. Ease of Access & Management

### Securely managing VMs across multiple accounts

The company's AWS footprint spans many accounts, the natural result of growth through acquisition, and
any credential capable of reaching into all of them is a serious liability if it leaks. This solution
avoids that risk entirely by never using long-lived credentials at all, and by keeping every account's
attack surface as small as possible.

All accounts are brought under a single **AWS Organization**. A **CloudFormation StackSet**, deployed
once from the organization's management account, rolls out a purpose-built IAM role,
**`AnsibleOpsRole`**, into every member account automatically, including accounts that join the
organization later, with no manual step required. This role is deliberately narrow: it can list EC2
instances and their tags, open Systems Manager sessions to instances that have not opted out of
monitoring, read and write to a single per-account S3 bucket used only for file hand-off, and create or
delete CloudWatch alarms whose names begin with `DiskUsage-`. It cannot touch anything else in that
account.

A single **Ansible controller**, running in a dedicated operations account, holds one identity of its
own, **`AnsibleControllerRole`**. That role has essentially no direct permissions; its only capability is
to call `sts:AssumeRole` on `AnsibleOpsRole` in any member account. When Ansible needs to act in a
member account, it assumes that account's `AnsibleOpsRole` and receives temporary, one-hour credentials
scoped to exactly the permissions above, nothing more.

This two-role handshake is the security boundary of the whole design. Reaching into a member account
requires two independent things to be true: the controller must be permitted to assume the role, and the
member account's role must, via its own trust policy, be willing to be assumed, a decision each account
owner controls and can revoke unilaterally by editing or removing their own role. The trust policy is
further restricted with an `aws:PrincipalOrgID` condition, so the role can only ever be assumed by a
principal inside the organization, even if its ARN were somehow discovered. No IAM user, no access key,
and no password appears anywhere in this chain.

### How Ansible connects to and collects data from VMs, reliably and securely

Ansible never opens an SSH connection to a monitored VM. Instead, every task is carried over **AWS
Systems Manager Session Manager**, using the `amazon.aws.aws_ssm` connection plugin. Each VM's own SSM
agent initiates an outbound HTTPS connection to the SSM service and keeps it open; the controller, having
assumed `AnsibleOpsRole`, asks SSM to relay a session to that VM. Nothing ever connects inbound to a VM.
The practical consequence is that no security group needs port 22, or any port, open to the controller;
VMs in fully private subnets are reached exactly the same way, provided they have a route to the SSM
service (via NAT or VPC interface endpoints); there are no SSH keys to generate, distribute, rotate, or
leak; and every single session is recorded in CloudTrail, giving a complete, tamper-evident audit trail of
every command Ansible ever ran and on whose authority.

Discovery of which VMs exist is likewise dynamic and credential-light. Ansible's `aws_ec2` inventory
plugin queries the EC2 API, per account and per region, using the same assumed `AnsibleOpsRole`
credentials, and automatically excludes any instance carrying the tag `Monitoring=disabled`, giving
individual teams a simple, explicit opt-out. Because file transfer is not something Session Manager is
built for, the small task payloads Ansible sends to a VM, and any files it retrieves, are handed off
through a private, encrypted, TLS-only S3 bucket that is provisioned per account and per region and that
automatically expires its contents after one day.

Reliability comes from two properties designed in from the start. Every Ansible task in this solution is
**idempotent**: re-running the enrollment playbook against a VM that is already correctly configured
changes nothing and reports no errors, which makes it safe to run repeatedly, whether triggered by a new
VM starting or by the periodic reconciliation pass described in Section 3. And because setup and
monitoring are decoupled, a VM whose agent was installed hours or days ago continues reporting data and
triggering alarms independently of whether the controller happens to be running at this exact moment.

---

## 2. Data Collection & Aggregation

### Gathering disk usage data from all VMs

Disk free space is not something AWS's native EC2 or EBS metrics expose; `AWS/EBS` reports throughput
and IOPS at the block-device level, but has no visibility into what a filesystem inside the VM has
actually used. Something running inside the guest operating system is required, and this solution uses
the **Amazon CloudWatch agent** for that purpose, since it is AWS-supported, requires no third-party
licensing, and integrates directly with the alerting and dashboarding already being used.

Ansible's `cloudwatch_agent` role installs this agent on every discovered VM, Linux or Windows, selecting
the correct package and installation method for the distribution in question, and writes it a small
configuration file that instructs it to report `disk_used_percent` and free inode count for every real
mount point, once every sixty seconds, while explicitly excluding pseudo-filesystems such as `tmpfs`,
`overlay`, and `squashfs` that would otherwise generate noisy, meaningless metrics and unnecessary cost.
This is a **push** model: each VM sends its own data outward to CloudWatch in its own account. Nothing
polls VMs for their disk usage, which means the monitoring itself has no single point of failure and
scales linearly with the number of VMs rather than with the capacity of any central poller.

### Centralizing and presenting the data for easy monitoring

Each metric a VM reports carries its instance ID as a dimension, and lands in a namespace called
`CWAgent` inside that VM's own account and region, alongside a set of **CloudWatch alarms** that Ansible's
`disk_alarms` role provisions for every real disk it finds: a warning alarm at 80% utilization, a
critical alarm at 90%, and a predictive alarm that uses CloudWatch's `RATE()` metric-math function to
project, from the current rate of growth, how many hours remain before a disk fills entirely, catching
a fast-filling disk long before it would otherwise cross either fixed threshold. All three alarm types
notify a single, central **SNS topic**, giving operators one place to subscribe for email, Slack, or
pager alerts, regardless of which account or region the underlying disk actually lives in.

Because metrics are otherwise confined to their own account, a **CloudWatch Observability Access Manager
(OAM)** link is established from every member account to a central sink in the operations account,
sharing metrics for cross-account querying without copying or duplicating the underlying data. On top of
that shared view sits a single **CloudWatch dashboard**, provisioned once as part of the initial
foundation and never touched again, that queries across every linked account for the fullest disks in
the fleet. Because the dashboard is a live query rather than a fixed list, it requires no maintenance as
new accounts and VMs are added; it simply shows more rows.

---

## 3. Scalability

### Handling growth as accounts and VMs are added over time

Three separate growth paths are addressed, since accounts, VMs, and time all introduce different kinds
of change.

**A new AWS account** joining the organization is picked up automatically. The CloudFormation StackSet
that provisions `AnsibleOpsRole`, the SSM file-transfer bucket, and the cross-account metrics link is
configured for **auto-deployment**: the moment an account lands in the target organizational unit,
CloudFormation deploys the same template into it with no human action. From that point forward, the
account is indistinguishable to Ansible from any other; the dynamic inventory picks it up the next time
it refreshes, and the dashboard begins including its metrics as soon as the link is established.

**A new VM** does not wait for a scheduled run to be discovered. Every VM entering the `running` state
fires an EC2 event, which is forwarded, via an EventBridge rule in its own account, to a central event
bus in the operations account, where it lands on an SQS queue. A small worker process on the controller
consumes that queue and runs the enrollment playbook against exactly the VM that just started, typically
enrolling it within a few minutes of boot. If a VM briefly fails to enroll, for instance because its SSM
agent has not yet finished registering, the message is retried automatically; if it fails ten times in a
row, it is moved to a dead-letter queue that itself raises an alarm, ensuring a VM that cannot be
monitored is never silently missed. An hourly reconciliation pass provides a second layer of assurance,
re-running the same idempotent enrollment logic against every known VM, so that VMs that were already
running before the pipeline was deployed, disks added to an existing VM after the fact, and any drift in
an agent's configuration are all corrected without manual intervention. A companion coverage report can
be run on demand to explicitly list any VM that is not currently sending metrics.

**Sustained growth in raw numbers** of accounts and VMs is handled by design choices made throughout the
solution rather than by any single mechanism: Ansible runs are batched (`serial`) so a large fleet is
converged incrementally rather than all at once; the inventory is partitioned per account and per region
so discovery parallelizes naturally; alarm and metric dimensions are kept minimal to control CloudWatch
cost as the number of monitored disks scales into the thousands; and because both the alarm rules and the
dashboard are queries rather than hardcoded lists, neither requires any change as the fleet grows.

---

## Key Components

**Access management.** A single, narrowly-scoped controller identity (`AnsibleControllerRole`) can do
nothing but request temporary credentials for a role (`AnsibleOpsRole`) deployed into every member
account by CloudFormation StackSets. That role's trust policy accepts only the controller, and only
principals inside the organization, and its permissions are limited to inventory reads, Session Manager
access to non-opted-out instances, and management of disk alarms it created itself. All access to VMs is
brokered through AWS Systems Manager Session Manager rather than SSH, eliminating inbound network access,
key management, and unaudited access as concerns entirely.

**VM Discovery and Enrollment for metric collection.** VMs are discovered dynamically per account and
region via Ansible's EC2 inventory plugin, with a tag-based opt-out. Enrollment, installing the CloudWatch
agent and provisioning its alarms, is event-driven: a VM's own "started" event triggers automatic,
near-real-time enrollment through an EventBridge-to-SQS pipeline consumed by a worker on the controller,
backed by an hourly reconciliation pass that guarantees eventual consistency for anything the event path
misses. Every enrollment action is idempotent, making both the event-driven and scheduled paths safe to
run repeatedly against the same VM without side effects.

---

## Repository layout

```
ansible.cfg  requirements.txt  collections/requirements.yml
inventory/accounts.yml            # accounts + regions (or --from-org)
inventory/generate_inventory.py   # -> per account/region aws_ec2 inventory + AWS profiles
playbooks/enroll.yml              # install agent + create alarms (idempotent)
playbooks/verify_coverage.yml     # "which VMs are NOT monitored?"
playbooks/simulate_fill.yml       # demo: fill a disk to trigger alarms
playbooks/group_vars/all.yml      # ops account id, SNS topic
roles/cloudwatch_agent/           # Linux + Windows agent install/config
roles/disk_alarms/                # threshold + predictive alarms
cloudformation/                   # controller, member-global, member-regional, monitoring-regional, enrollment-events
scripts/deploy_foundation.sh      # one-time bootstrap (StackSets)
scripts/enroll_worker.py          # event-driven enrollment worker (SQS -> ansible-playbook)
scripts/reconcile.sh              # hourly safety-net (systemd timer)
scripts/coverage_report.py        # used by verify_coverage.yml
tests/                            # unit tests for the enrollment worker
```

## Sandbox Setup 

```bash
cd sandbox
pip3 install -r requirements.txt
# edit sandbox.yaml: region, alert_email, email_base
python3 sandbox.py --dry-run init      # preview only
python3 sandbox.py init --create-org   # real run (skips org creation since one exists)
python3 sandbox.py apply               # creates the test VM(s)
python3 sandbox.py push-repo

python3 sandbox.py shell
sudo su - ec2-user

echo $AWS_CONFIG_FILE
echo 'export AWS_CONFIG_FILE=/opt/aws-disk-monitoring/inventory/aws_config' >> ~/.bashrc && source ~/.bashrc && echo $AWS_CONFIG_FILE
```bash
ansible-galaxy collection install \
  -r /opt/aws-disk-monitoring/collections/requirements.yml \
  -p /opt/aws-disk-monitoring/collections \
  --force
ls /opt/aws-disk-monitoring/collections/ansible_collections/amazon/aws
export AWS_CONFIG_FILE=/opt/aws-disk-monitoring/inventory/aws_config
sed -i \
  -e 's|(out / "aws_config").write_text("\\n".join(profiles))|aws_config_path = pathlib.Path("inventory/aws_config"); aws_config_path.write_text("\\n".join(profiles))|' \
  -e 's|print(f"next: export AWS_CONFIG_FILE={out.resolve()}/aws_config")|print(f"next: export AWS_CONFIG_FILE={aws_config_path.resolve()}")|' \
  inventory/generate_inventory.py
python3 inventory/generate_inventory.py

cd /opt/aws-disk-monitoring
```

## Sandbox Simulate

Disk Fill-up
```bash
ansible-playbook playbooks/enroll.yml -l <instance_id>
ansible-playbook playbooks/simulate_fill.yml -l <instance_id> -e confirm=yes -e fill_percent=85
ansible-playbook playbooks/simulate_fill.yml -l <instance_id> -e confirm=yes -e cleanup=true
```

Disk restart
```bash
aws ec2 stop-instances --instance-ids i-0024ac42850274dfb --region us-east-1
aws ec2 wait instance-stopped --instance-ids i-0024ac42850274dfb --region us-east-1
aws ec2 start-instances --instance-ids i-0024ac42850274dfb --region us-east-1
```

## Sandbox Destroy

```bash
python3 sandbox.py destroy
```

