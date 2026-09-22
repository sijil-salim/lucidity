# Architecture in plain words

## The goal
Know when any VM's disk is filling up, in any AWS account, before the application breaks.

## The four things that matter
| # | Thing | Job |
|---|-------|-----|
| 1 | **A small reporter on every VM** (CloudWatch agent) | Measures how full each disk is and sends the number to CloudWatch every minute |
| 2 | **CloudWatch in each account** | Stores the numbers; **alarms** go off at 80 % / 90 % / "will be full in 24 h" |
| 3 | **Ansible, running on one "controller" server** | The installer: reaches every VM, installs the reporter, creates the alarms |
| 4 | **One alert channel + one dashboard** (SNS topic + CloudWatch dashboard, ops account) | Alarms message one place (email/Slack). One dashboard shows the fullest disks everywhere |

Everything else in the repo is *plumbing* that makes those four safe and automatic:

| Plumbing | Why it exists |
|----------|---------------|
| **SSM Session Manager** | The safe tunnel Ansible uses to reach VMs: no SSH keys, no open ports |
| **IAM roles** (controller role, AnsibleOpsRole, instance role) | Permission slips: who may do what. See `README.md` |
| **StackSets** | Auto-installs the roles/bucket into every new account so nobody does it by hand |
| **OAM (CloudWatch cross-account)** | Lets the ops-account dashboard *see* other accounts' metrics |
| **S3 bucket per account** | Mailbox for handing small files to VMs over SSM |

## The flow, in three phases

### Phase A - once (foundation)
```
Create the roles / buckets / SNS topic / dashboard  (CloudFormation, StackSets for many accounts)
```

### Phase B - SETUP, not tracking (triggered by a VM starting, plus an hourly safety-net run)
```
Trigger: a VM starts -> EventBridge event -> event bus -> queue -> worker on the controller
Then, for that VM (or for everything, in the hourly safety-net run):
Controller --(1) "who exists?"----------> asks EC2 in each account: list running VMs
Controller --(2) "let me in"------------> SSM tunnel into each VM (permission: AnsibleOpsRole)
Controller --(3) on each VM-------------> install reporter, write its config
Controller --(4) per disk---------------> create CloudWatch alarms in that VM's account
```
Re-running is safe: things already done are skipped (idempotent). **Ansible never reads disk usage.**
It only makes sure every VM has the reporter and alarms. If a VM is not ready yet (still registering with SSM), the
attempt is retried automatically; if it never succeeds, an alarm tells you that VM is unmonitored.

### Phase C - 24/7 TRACKING, no Ansible, no cron (this is the real monitoring)
```
VM (reporter) --every minute--> CloudWatch (own account)
CloudWatch alarm --disk 80/90 % or filling fast--> SNS topic --> email / Slack / pager
Dashboard (ops account) <-- reads metrics of every account via OAM
```
Key point: monitoring keeps working even if the Ansible controller is down. Ansible only
*sets things up*; the VMs and CloudWatch do the watching.
