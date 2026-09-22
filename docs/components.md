# Components explained (Organizations, StackSets, IAM roles, S3 and the rest)

Read [architecture-simple.md](architecture-simple.md) first for the flow. This page explains
every building block, where it lives, who creates it, and what it holds.

---
## 1. AWS Organizations, OUs and the management account (from zero)

An **AWS Organization** is one umbrella over many AWS accounts. Think of a company with many
bank accounts (one per team) and a head office that pays the bills.

| Term | Plain meaning | Looks like |
|---|---|---|
| **Organization** | The umbrella | id `o-ab12cd34ef` |
| **Management account** | The head-office account: creates the org, pays, can create accounts and StackSets | one account |
| **Member account** | Any other account in the org (a team, an app, an acquired company) | 12-digit id |
| **Root** | The top of the tree | `r-ab12` |
| **OU (Organizational Unit)** | A **folder** that groups accounts, so you can act on a whole group at once | `ou-ab12-cd34ef56` |
| **SCP** | A guard-rail policy applied to an OU/account (not used in this solution) | - |

```
Root (r-ab12)
 |- OU: Workloads
 |    |- OU: Prod  -> accounts: prod-payments, prod-platform
 |    '- OU: Dev   -> account:  dev-sandbox
 |- OU: Security  -> account:  log-archive
 '- Management account  (head office)
```

**Why an OU matters to us:** "deploy this to every account in OU Prod" is one instruction. When a new
account is moved into that OU, it can receive the deployment automatically (see StackSets).

**Where this solution uses the Organization (4 places):**
1. `aws:PrincipalOrgID` condition in `AnsibleOpsRole`'s trust policy: "only principals from accounts in *my* org may assume me".
2. StackSets target OUs and auto-deploy to new accounts.
3. The OAM sink policy: "only accounts in my org may link their metrics to me".
4. `generate_inventory.py --from-org` lists all ACTIVE accounts (`organizations:ListAccounts`).

---
## 2. CloudFormation StackSets (from zero)

* A **stack** = one CloudFormation template deployed in **one account + one region**.
* A **StackSet** = one template deployed as **many stacks** across many accounts/regions. Each
  individual stack it creates is a **stack instance**.
* **Service-managed permissions** (what we use): CloudFormation uses AWS Organizations, so you
  target an **OU** and no per-account admin/execution roles are needed. AWS creates its own
  service-linked roles for this (names start with `AWSServiceRoleForCloudFormationStackSets...`).
* **Auto-deployment:** when a new account joins the target OU, CloudFormation automatically creates the
  stack instance there (and can remove it when the account leaves). **This is what makes "add an account and it
  just works" true.**
* Prerequisites: *trusted access* for StackSets enabled in Organizations; run from the management
  account (or a delegated administrator). Service-managed StackSets do not deploy into the management
  account itself - deploy there with an ordinary stack.

**Our two StackSets and why there are two**

| StackSet | Template | One stack instance per... | Creates |
|---|---|---|---|
| `disk-mon-member-global` | `member-global.yaml` | account (IAM is global, so deploy in one region) | `AnsibleOpsRole`, SSM default-management role |
| `disk-mon-member-regional` | `member-regional.yaml` | account **x region** | transfer S3 bucket, SSM setting, OAM link |

Regional depends on global (the setting uses the role), so global is deployed first.
In the ops account we use **plain stacks** (not StackSets): `ansible-controller` and, per region, `disk-monitoring-hub`.

**Timeline for a new account** ("team-x"): moved into OU *Prod* -> within minutes StackSets create the
role + bucket + link in it -> you (or a scheduled job) run `generate_inventory.py --from-org` -> it
appears in the inventory -> `enroll.yml` installs the agent on its VMs.

---
## 3. Every IAM role in the design

| # | Role | Lives in | Assumed / used by | Purpose | Defined in |
|---|---|---|---|---|---|
| 1 | **`AnsibleControllerRole`** | ops account (1) | The Ansible controller EC2 (via instance profile `AnsibleControllerProfile`) | The controller's own identity. It can **only** call `sts:AssumeRole` on `AnsibleOpsRole` (any account), list Org accounts, and **read the enrollment queue**. (Also SSM core so you can reach the controller with Session Manager) | `controller.yaml` (+ queue read: `enrollment-events.yaml`) |
| 2 | **`AnsibleOpsRole`** | every member account (N) | Ansible, after assuming from role 1 | The "worker" identity in that account: read inventory, open SSM sessions to tagged VMs, use the transfer bucket, create/delete `DiskUsage-*` alarms. Nothing else | `member-global.yaml` |
| 3 | **`AWSSystemsManagerDefaultEC2InstanceManagementRole`** | every member account (N) | The SSM service (Default Host Management) | Lets VMs that have **no** instance profile still register with SSM. Optional convenience | `member-global.yaml` |
| 4 | **VM instance role** (e.g. `VmRole`) | every account, attached to VMs | The VM (SSM agent + CloudWatch agent) | `AmazonSSMManagedInstanceCore` (register with SSM) + `CloudWatchAgentServerPolicy` (**publish metrics**) | **Not in the repo** (a known gap); the sandbox creates it |
| 5 | **`EnrollmentForwardRole`** (auto-named) | every member account, per region | The EventBridge service | Lets the member account's rule put "VM started" events onto the central bus in the ops account. Only `events:PutEvents` on that bus | `member-regional.yaml` |
| 6 | AWS service-linked roles | management + members | CloudFormation StackSets, OAM | Created by AWS when you enable those features | AWS |
| 7 | `OrganizationAccountAccessRole` | accounts created via Organizations | **Sandbox tool only** | Admin access from the management account into accounts the sandbox creates | AWS (automatic) |

So: **4 kinds of role are ours** (1, 2, 3, 5), **1 more is required on the VMs** (4), and AWS adds a couple of its own.

**The chain (two hops) and the two locks** (you know this from the SAA):
```
Controller EC2 --instance profile--> credentials of AnsibleControllerRole (role 1)
  --sts:AssumeRole--> temporary credentials of AnsibleOpsRole in account B (role 2, 1 hour)
      --ssm:StartSession / DescribeInstances / PutMetricAlarm...--> account B
```
Cross-account assume needs **both** locks:
* Role 1's **identity policy** allows `sts:AssumeRole` on `arn:aws:iam::*:role/AnsibleOpsRole`.
* Role 2's **trust policy** names role 1's ARN as principal **and** requires `aws:PrincipalOrgID = <our org>`.

Why the org-ID condition and not an ExternalId? ExternalId defends against a confused-deputy attack by a
*third party*. Here every account is ours, so "principal must be inside our org" is the right control.

Role 2's permissions, exactly: `ec2:DescribeInstances/Tags`, `ssm:DescribeInstanceInformation`,
`cloudwatch:ListMetrics/DescribeAlarms` (read); `ssm:StartSession` only on instances **not** tagged
`Monitoring=disabled` (+ the session documents); terminate/resume own sessions; S3 get/put/delete on
`ansible-ssm-<account>-*`; `cloudwatch:PutMetricAlarm/DeleteAlarms` only on `alarm:DiskUsage-*`.

---
## 4. S3: everything that is (and is not) stored

| Bucket | Created by | What it holds | Lifetime |
|---|---|---|---|
| **`ansible-ssm-<account>-<region>`** (one per account per region) | `member-regional.yaml` | **Temporary hand-off files for the Ansible SSM connection**: the small Ansible task programs it uploads, files it copies to a VM (e.g. the rendered CloudWatch config), files it fetches back. Keyed by instance id | Auto-deleted after **1 day** (lifecycle rule) |
| `amazoncloudwatch-agent-<region>` | **AWS** (not ours) | The CloudWatch agent installers (`.deb`, `.rpm`, `.msi`) that non-Amazon-Linux VMs download | AWS-managed, read-only for us |
| `<repo bucket>` | **Sandbox only** | `repo.zip`, the copy of this repo pushed to the controller | Until `destroy` |

**The third bucket (sandbox only) in one line:** your test controller lives in AWS but the repo is on your laptop, and there is
no SSH. `sandbox.py push-repo` zips the repo, uploads it to this bucket, and tells the controller (over SSM) to download and
unzip it. It is a delivery box for the test lab and is **not part of the real solution**.

**Why S3 at all?** Session Manager is like a terminal: it is not built to move files. The
`amazon.aws.aws_ssm` plugin therefore uploads what it needs to the bucket, and the VM downloads it with a
short-lived **presigned URL** (so the VM's own role needs no S3 permission). That is why the ops role has S3
rights and why the bucket must be reachable from the VM (S3 gateway endpoint or NAT).

Bucket hardening: encryption on (AES-256), all public access blocked, policy denies non-TLS, 1-day expiry.
Treat the contents as sensitive (rendered configs can contain values) - hence private + short-lived.

**Not stored in S3:** the disk metrics (they go to CloudWatch), alarm data, or logs.
*Optional, not implemented:* send Session Manager session logs to S3/CloudWatch Logs for audit.

---
## 5. All the other components

| Component | Lives in | Created by | What it does / holds |
|---|---|---|---|
| **Ansible controller** (EC2 or AWX) | ops account | you (sandbox creates one) | Runs playbooks. Holds this repo, Ansible, boto3, the `session-manager-plugin`. **No long-lived keys** - only role 1 |
| **Inventory files** | controller | `generate_inventory.py` | One `aws_ec2` file per account+region + `aws_config` (a profile per account). Generated, not committed |
| **SSM agent** | every VM | preinstalled on AWS images | Keeps an outbound connection to SSM so Session Manager works |
| **Session Manager / DHMC** | each account+region | AWS / `member-*.yaml` | The no-SSH tunnel; DHMC registers profile-less VMs |
| **CloudWatch agent** | every VM | `cloudwatch_agent` role | Reads disk usage from the OS every 60 s and pushes it. Config at `/opt/aws/amazon-cloudwatch-agent/etc/disk-monitoring.json` |
| **CloudWatch metrics** (`CWAgent` namespace) | each VM's own account | the agent | `disk_used_percent`, `inodes_free` per disk (dimensions `InstanceId`, `path`, `fstype`); Windows: `LogicalDisk % Free Space` |
| **CloudWatch alarms** (`DiskUsage-...`) | each VM's own account+region | `disk_alarms` role | **3 per disk**: warning 80 %, critical 90 %, predicted-full-in-24 h |
| **SNS topic `disk-alerts`** | ops account, **each region in use** | `monitoring-regional.yaml` | Receives every alarm; fan out to email/Slack/PagerDuty. Must be same-region as the alarm |
| **OAM sink** | ops account, per region | `monitoring-regional.yaml` | "Central inbox" for other accounts' metrics |
| **OAM link** | each member account, per region | `member-regional.yaml` | Shares that account's metrics to the sink (data is queried in place, not copied) |
| **Dashboard `fleet-disk-utilization`** | ops account, per region | `monitoring-regional.yaml` | Top-10 fullest disks across linked accounts |
| **EventBridge rule (member)** | each member account, per region | `member-regional.yaml` | Matches "EC2 instance state = running" and forwards it to the central bus |
| **Central event bus** `disk-monitoring-events` | ops account, per region | `enrollment-events.yaml` | Receives those events from all accounts of our org (bus policy uses `aws:PrincipalOrgID`) |
| **SQS queue** `disk-monitoring-enrollment` + **DLQ** | ops account, per region | `enrollment-events.yaml` | Holds one message per started VM until the worker enrolls it; messages that fail 10 times go to the DLQ, which raises an alarm |
| **Enrollment worker** (`enroll_worker.py`, systemd) | controller | you install it (sandbox does) | Reads the queue, batches VMs, runs `enroll.yml -l <ids>`, retries/skips as needed |
| **Hourly safety net** (`reconcile.sh`, systemd timer = cron) | controller | you enable it (sandbox does) | Rediscovers, re-runs enrollment, reports unmonitored VMs. Catches missed events, new disks, drift |
| **Tags** | EC2 instances | your teams | `Monitoring=disabled` opts out; `Environment` becomes an Ansible group (`env_prod`) |

---
## 5b. OAM = CloudWatch **O**bservability **A**ccess **M**anager
**The problem:** CloudWatch data normally stays inside its own account and region. To see disks in 50 accounts you would log into 50 accounts.

**The idea (control-room analogy):** each building (member account) has cameras (metrics). One control room (the ops account)
wants to watch all of them without moving the cameras.

| Piece | Where | What it is |
|---|---|---|
| **Sink** | ops/monitoring account, per region | The control room's receiving desk. Its policy says *who may connect*: ours allows principals of our org, metrics only |
| **Link** | each member account, per region | The cable from a building to the desk: "share my metrics with that sink" |

Once linked, the ops account's dashboard and Metrics console can pick a **source account** and graph its metrics. The data is
**queried in place, not copied**. OAM can also share logs and traces; we share metrics only. Our alarms still live in the member
accounts; OAM only gives central *visibility*. Sinks/links are per region, so we deploy one of each per region in use. The ops
account cannot link to itself, which is why the `home` account skips the link.

---
## 5c. Controller role vs Ops role
| | `AnsibleControllerRole` | `AnsibleOpsRole` |
|---|---|---|
| How many | 1 (ops account) | 1 per member account |
| Who uses it | The controller server, automatically (instance profile) | Ansible, after assuming it |
| What it can do | **Almost nothing**: only ask for `AnsibleOpsRole` (+ list Org accounts) | The real work in *that* account: list VMs, open SSM sessions, manage `DiskUsage-*` alarms, use the transfer bucket |
| Credentials | Long-running, attached to the server | Temporary (1 hour), fetched as needed |
| Who controls it | Ops account | The **member account** itself (it owns the role and its trust policy) |

Analogy: the controller role is an employee ID badge that opens no doors, only lets you *request* a visitor pass. The ops role is the
visitor pass issued by each building, and each building decides which doors it opens. To act in another AWS account you always need a
role *in that account* that trusts you; this is that pattern.

---
## 6. Back-of-envelope numbers (handy in an interview)
Example: 5 accounts, 100 VMs, 2 real disks each, 1 region.
* IAM roles: 1 controller + 5 ops roles + 5 DHMC roles + 5 event-forward roles (+ VM instance roles).
* S3 buckets: 5 transfer buckets. StackSets: 2 (= 5 + 5 stack instances). OAM links: 5 (or 4 + the ops account itself).
* Metrics: 100 x 2 disks x 2 measurements = 400 custom metrics. Alarms: 100 x 2 x 3 = **600**.
* Cost is dominated by custom metrics and alarms (roughly $0.30 per custom metric-month and $0.10 per standard
  alarm-month at the time of writing - verify current pricing). Filtering pseudo filesystems and using `drop_device`
  keeps the count down. Check Service Quotas for per-region alarm limits.

---
## 7. Interview answers in 30 seconds
* **"What's an OU / why StackSets?"** An OU is a folder of accounts. Service-managed StackSets deploy one
  template to every account in an OU and auto-deploy to accounts that join later, so onboarding needs no manual work.
* **"How is cross-account access secured?"** The controller has one role that can only assume `AnsibleOpsRole`.
  That role trusts only the controller and only principals in our org, has least-privilege permissions,
  and issues 1-hour credentials. VMs are reached via SSM: no keys, no inbound ports, every session in CloudTrail.
* **"What's in S3?"** Only temporary hand-off files for the Ansible-over-SSM connection (1-day expiry). Metrics are in CloudWatch.
* **"How many roles?"** Ours: controller (1), ops role per account, DHMC per account; plus the instance role on
  each VM for SSM + CloudWatch agent permissions; AWS adds service-linked roles.
* **"New account joins?"** StackSet auto-deploys role/bucket/link; inventory picks it up with `--from-org`;
  the next `enroll.yml` run installs agents and alarms.
* **"How does a new VM get monitored?"** Automatically: its `running` event is forwarded to a central bus and queue; a worker
  on the controller runs the idempotent enrollment for it within minutes. Failures retry, then alarm via a dead-letter queue;
  an hourly reconcile run (systemd timer) catches VMs that were already running, new disks and drift. Disk tracking itself is continuous and never depends on a schedule.
* **"Is monitoring cron-based?"** No. Agents push every 60 s and CloudWatch alarms evaluate continuously; SNS fires on state change.
  Ansible only does setup.
* **"Why not just SSM without Ansible?"** It could do much of it. Ansible is the mandated existing tool,
  is multi-cloud, and keeps configuration in Git. Say so honestly.
