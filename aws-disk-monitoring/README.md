# Multi-account disk-utilization monitoring on AWS (Ansible + SSM + CloudWatch)

Detect low disk space across **every VM in every AWS account** before it causes downtime,
using the existing Ansible stack plus cloud-native services only where they add clear value.

**Diagram:** [docs/architecture.md](docs/architecture.md) (Mermaid - renders on GitHub)
**New to this? Start here:** [docs/architecture-simple.md](docs/architecture-simple.md) (the flow in plain words)
**Every component, IAM role and S3 bucket explained:** [docs/components.md](docs/components.md)
**Test it for real:** see "Testing it: the sandbox" below (the `sandbox/` folder builds a multi-account test lab).

## Design in one paragraph
Ansible reaches VMs through **SSM Session Manager** (no SSH, no inbound ports, no bastions) using a
**least-privilege cross-account role** rolled out to every account by **CloudFormation StackSets**.
Ansible **discovers** VMs with the `aws_ec2` dynamic inventory and **enrolls** them by installing and
configuring the **CloudWatch agent**, which *pushes* disk metrics. Ansible also creates per-disk
**alarms** (80 % / 90 % / predictive "full within 24 h"). **CloudWatch Observability Access Manager
(OAM)** shows all accounts in one central dashboard; alarms notify one **SNS** topic.

## Key components (summary)
**Access management.** One controller identity can only assume `AnsibleOpsRole` in member accounts;
that role trusts only the controller and only principals in our AWS Organization, is limited to
read-inventory / Session Manager / disk alarms, and uses short-lived credentials. VMs are reached
through SSM Session Manager: no SSH keys, no inbound ports, no bastions.

**VM discovery and enrollment.** The `aws_ec2` dynamic inventory lists running VMs per account and
region (opt-out with tag `Monitoring=disabled`). `enroll.yml` installs the CloudWatch agent on each,
writes its disk config, and creates per-disk alarms. It is idempotent, so it can run on a schedule.

## Why these choices
| Decision | Reason |
|---|---|
| SSM instead of SSH | No port 22, no key sprawl, every session in CloudTrail, works in private subnets |
| Agent **push** instead of Ansible **poll** | Ansible is the *configurator*, not the monitor: a controller outage must not blind monitoring; scales with the fleet, not with controller capacity |
| CloudWatch (cloud-native) | Substantial benefit: zero infra to run, native alarms, cross-account view via OAM. Avoids a third-party platform, as leadership asked |
| StackSets (service-managed, auto-deploy) | New accounts get the role automatically - onboarding needs no human |
| Alarms created from each VM's actual mounts | New/extra disks are picked up on the next converge run |

## Repo layout
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
cloudformation/                   # controller, member-global, member-regional, monitoring-regional
scripts/deploy_foundation.sh      # one-time bootstrap (StackSets)
scripts/coverage_report.py        # used by verify_coverage.yml
scripts/enroll_worker.py          # event-driven enrollment: SQS queue -> ansible enroll.yml -l <new VM>
scripts/reconcile.sh + systemd/   # worker service + hourly safety-net timer (reconcile)
cloudformation/enrollment-events.yaml  # central event bus + queue (+ alarm if an enrollment gets stuck)
tests/                            # unit tests for the worker (python -m unittest discover tests)
```

## Quick start
```bash
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
# also install the AWS session-manager-plugin on the controller

# 1. One-time foundation (review the script first)
ORG_ID=o-xxxxxxxxxx ORG_ROOT_ID=r-xxxx ./scripts/deploy_foundation.sh

# 2. Edit playbooks/group_vars/all.yml (ops_account_id) and inventory/accounts.yml
python inventory/generate_inventory.py --from-org
export AWS_CONFIG_FILE=$PWD/inventory/aws_config

# 3. Enroll (start with one account/environment)
ansible-inventory --graph
ansible-playbook playbooks/enroll.yml -l account_prod_payments

# 4. Prove it works
ansible-playbook playbooks/simulate_fill.yml -l i-0abc... -e confirm=yes -e fill_percent=85
ansible-playbook playbooks/simulate_fill.yml -l i-0abc... -e confirm=yes -e cleanup=true
ansible-playbook playbooks/verify_coverage.yml
```

## 1. Ease of access & management
* **Cross-account:** controller has one identity (`AnsibleControllerRole`) that can only `sts:AssumeRole`
  into `AnsibleOpsRole`. That role's trust policy names the controller role **and** requires
  `aws:PrincipalOrgID`. Sessions are short-lived (1 h); there are no long-lived keys anywhere.
* **Least privilege:** the ops role can read EC2/SSM/CloudWatch inventory, `ssm:StartSession` only on
  instances **not** tagged `Monitoring=disabled`, use the transfer bucket, and create/delete only alarms
  named `DiskUsage-*`. It cannot touch anything else.
* **Ansible -> VM:** the `amazon.aws.aws_ssm` connection plugin, using the per-account profile written by
  `generate_inventory.py`. Transport is encrypted by SSM; files move via a per-account, private,
  1-day-expiry S3 bucket.
* **Instances with no instance profile:** SSM *Default Host Management Configuration* is enabled by the
  StackSet so they can still register with SSM.

## 2. Data collection & aggregation
* **Collect:** CloudWatch agent, 60 s interval, `disk_used_percent` + `inodes_free` (Linux), `LogicalDisk % Free Space` (Windows). Pseudo filesystems (tmpfs, overlay, squashfs...) are excluded to control noise and cost.
* **Centralize:** OAM sink in the ops account, link in each member account -> one dashboard
  (`fleet-disk-utilization`, top-10 fullest disks) without copying metrics.
* **Alert:** `warning >= 80 %` (3 of 5 minutes, ~3 min to alert), `critical >= 90 %` (2 of 2 minutes, ~2 min), plus a predictive
  alarm using `RATE()` that fires when a disk is projected to fill within 12 h (timing is tunable via `disk_thresholds`) (catches fast-filling disks the thresholds would miss).
  All go to the central SNS topic (wire to Slack/PagerDuty/email).
* **Assure:** `verify_coverage.yml` reports VMs that are unreachable by SSM or not sending metrics.

## 3. Scalability
* **New account:** StackSet auto-deploys role + bucket + OAM link; add it to the inventory
  (`--from-org` does this automatically) and it is picked up on the next run.
* **New VM:** enrolled **by event** within minutes of starting (see below); an hourly reconcile run catches anything events cannot
  (VMs already running, new disks, drift). Opt out with tag `Monitoring=disabled`.
* **New disk:** the next converge run creates its alarms.
* **Large fleets:** `serial` batching (`-e enroll_batch_size=10`), `forks`, one inventory file per
  account/region (parallel discovery), agent-push data plane.
* **Cost:** custom metrics are per-disk; `drop_device` and the fstype filter keep dimensions minimal.

## How a new VM gets monitored (event-driven; no dependence on cron)
Disk **tracking** never depends on a schedule: agents push every 60 s and CloudWatch alarms evaluate continuously.
Only **enrollment** (installing the agent + alarms on a *new* VM) needs a trigger, and that is event-driven:

1. A VM enters `running` in any account -> an EventBridge rule (deployed to every member account by `member-regional.yaml`)
   forwards the event to a central **event bus** in the ops account.
2. A rule on that bus puts it on an **SQS queue** (`disk-monitoring-enrollment`, with a dead-letter queue).
3. `scripts/enroll_worker.py` on the controller reads the queue, batches up to 10 VMs and runs `enroll.yml -l <ids>`.
4. VM booted but not yet registered with SSM -> the run fails -> the message is retried after ~2 min, up to 10 times ->
   then it lands in the DLQ and an alarm fires (**an unmonitored VM never goes unnoticed**).
5. VM opted out or unknown: skipped; unknown account: inventory is refreshed once and retried.

An **hourly safety net** (`scripts/reconcile.sh`, run by `reconcile.timer`, a systemd timer i.e. cron) rediscovers accounts/VMs,
re-runs the idempotent enrollment for everything and prints the coverage report. It covers what events cannot: VMs that were
*already running* at go-live or when an account was onboarded (no event), **new disks** on existing VMs, and a stopped agent or
drifted config (re-running enrollment repairs both). Events are the fast path; the timer is the safety net.

Install on the controller (the sandbox does this for you in `push-repo`):
```bash
printf 'QUEUE_URL=<queue url from enrollment-events stack>\nAWS_DEFAULT_REGION=<region>\nUSE_ORG=1\n' | sudo tee /etc/disk-monitoring.env
sudo cp scripts/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now enroll-worker.service reconcile.timer
# test without AWS:  python3 scripts/enroll_worker.py --event-file tests/sample-ec2-running.json --dry-run
```
Limits to know: a disk that fills in under a minute cannot be caught by any threshold monitor (keep headroom, use the
predictive alarm, or expand disks proactively); the worker runs one Ansible batch at a time.

## Testing it: the sandbox
The `sandbox/` folder (a sibling of this one) builds a throw-away multi-account lab so the solution can be
run for real: an Ansible controller server, any number of AWS accounts, and any number of test VMs in each.
You describe the lab in `sandbox/sandbox.yaml`; `sandbox/sandbox.py` makes AWS match it. Full guide: `sandbox/README.md`.

```bash
cd sandbox && pip install -r requirements.txt
./sandbox.py --dry-run init       # prints every AWS call, changes nothing - always look first
./sandbox.py init --create-org    # controller server, alert topic, dashboard (use --create-org if no Organization yet)
./sandbox.py apply                # create the accounts + VMs listed in sandbox.yaml
./sandbox.py push-repo            # copy this repo onto the controller
./sandbox.py shell                # open a shell on the controller, then follow sandbox/README.md
```
| I want to... | Command |
|---|---|
| See what exists | `./sandbox.py list` |
| Add a new AWS account / adopt an existing one | `./sandbox.py add-account team-b` / `add-account team-b --id 123456789012` |
| Add VMs (5 Ubuntu, tagged prod) | `./sandbox.py add-vm team-b api --count 5 --os ubuntu --env prod` |
| Add a Windows VM / an opted-out VM | `add-vm team-b win --os windows --type t3.medium` / `add-vm team-b quiet --no-monitoring` |
| Remove a VM / an account | `./sandbox.py remove-vm team-b api-3` / `remove-account team-b` |
| Delete everything | `./sandbox.py destroy` |

The default lab has only the `home` account (no new AWS accounts are created). After every change run
`./sandbox.py push-repo` so the controller sees it. Status: dry-run and template-lint tested only; run it for real in a
throw-away AWS account.

## Known limitations / next steps (honest list)
* **Reconcile limits:** a new disk is alarmed on the next hourly run (up to 1 h); on very large fleets the full run is slow (tune
  `serial`/`forks`, or the `OnCalendar` schedule in `scripts/systemd/reconcile.timer`). Its coverage report only lands in the journal
  (`journalctl -u reconcile`); alerting on the gaps that remain (e.g. publish to SNS) is a next step.
* **Instance permissions:** DHMC gives SSM access only. To *publish metrics* an instance's role needs
  `CloudWatchAgentServerPolicy`. Bake it into your standard instance profile / launch templates; a
  follow-up playbook could attach it to existing roles.
* Alarm SNS policy is scoped by alarm-name prefix; tighten with `aws:SourceOrgID` if supported.
* Non-Amazon Linux package install skips signature verification (marked TODO in the role).
* Windows alarms cover drives listed in `windows_drives` (default `C:`).
* Alarms for terminated instances are not auto-removed; add a cleanup job (list `DiskUsage-*`, delete
  those whose instance ID no longer exists).
* Ansible must reach a VM at least once to enroll it; alarm state for a VM whose agent later dies
  shows `INSUFFICIENT_DATA` - `verify_coverage.yml` is the backstop.
