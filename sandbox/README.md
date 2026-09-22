# Sandbox: a small multi-account AWS lab for the disk-monitoring solution

**What it does:** builds a throw-away test environment so you can run `../aws-disk-monitoring`
for real - an Ansible controller server, any number of AWS accounts, and any number of test VMs
in each. You describe what you want in `sandbox.yaml`; `sandbox.py` makes AWS match it.

> Not tested against real AWS by the author (only `--dry-run` and template linting).
> Expect to fix small things on the first real run. **Use a throw-away AWS account.**

## What you need
* An AWS account you can experiment in, with **admin credentials** in your terminal
  (`aws sts get-caller-identity` must work). This account becomes the *ops/home* account.
* AWS CLI v2, Python 3.9+, `pip install -r requirements.txt` (only PyYAML)
* For `shell`: the [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) on your own machine

## The picture
```
your laptop --sandbox.py--> AWS Organizations (your account = management/ops account)
                              |-- home account   : Ansible controller, alert topic, dashboard + test VMs
                              |-- prod-payments  : (optional real account) test VMs
                              '-- ...more accounts you add
```
Test VMs have **no inbound ports**: you can only reach them through SSM, exactly like production.

## First run (about 15 minutes)
```bash
cd sandbox
pip install -r requirements.txt
# edit sandbox.yaml: set region, alert_email (optional), email_base (only needed for NEW accounts)
./sandbox.py --dry-run init      # ALWAYS look first: prints every AWS call, changes nothing
./sandbox.py init --create-org   # --create-org only if the account is not in an Organization yet
./sandbox.py apply               # creates the accounts + VMs listed in sandbox.yaml
./sandbox.py push-repo           # copies ../aws-disk-monitoring onto the controller
./sandbox.py shell               # opens a shell on the controller
```
The default `sandbox.yaml` has only the `home` account with 2 VMs, so the first run creates
**no new AWS accounts**.

## On the controller (after `./sandbox.py shell`)
```bash
sudo su - ec2-user
cat /var/lib/sandbox-controller-ready        # exists once the boot script has finished (~3-5 min after init)
cd /opt/aws-disk-monitoring
ansible-galaxy collection install -r collections/requirements.yml
python3 inventory/generate_inventory.py
export AWS_CONFIG_FILE=$PWD/inventory/aws_config
aws sts get-caller-identity --profile ansible-home     # proves the role hop works
ansible-inventory --graph                              # should list your VMs
ansible all -m ansible.builtin.ping                    # proves the SSM connection
ansible-playbook playbooks/enroll.yml                  # install agent + create alarms
ansible-playbook playbooks/enroll.yml                  # run again: nothing should change
ansible-playbook playbooks/verify_coverage.yml
# trigger an alarm (use ONE test VM id from ansible-inventory):
ansible-playbook playbooks/simulate_fill.yml -l i-0abc... -e confirm=yes -e fill_percent=85
ansible-playbook playbooks/simulate_fill.yml -l i-0abc... -e confirm=yes -e cleanup=true
```
`push-repo` rewrites `inventory/accounts.yml` and `ops_account_id` **inside the copy it uploads**,
using the accounts in your sandbox. Your working repo files are never modified.

## Try the event-driven enrollment
`push-repo` also installs and starts the **enrollment worker** and enables the hourly **reconcile timer** (the safety net) on the controller. To see a new VM
get monitored without running any playbook yourself:
```bash
./sandbox.py add-vm home late-1            # on your laptop: a brand-new VM
./sandbox.py shell                         # then on the controller:
sudo journalctl -u enroll-worker -f        # within ~2-6 min you should see: ansible-playbook ... -l i-0abc...  then  "i-0abc...: ok"
```
Early attempts may say "retry": the VM is still registering with SSM; the worker retries every ~2 minutes automatically.
Then check CloudWatch -> Metrics -> `CWAgent` for the new instance id, and `aws cloudwatch describe-alarms --alarm-name-prefix DiskUsage-`.
Stuck enrollments end up in the dead-letter queue `disk-monitoring-enrollment-dlq` (an alarm is sent to the alert topic).

### The hourly safety net (cron)
`reconcile.timer` (a systemd timer, i.e. cron) runs `scripts/reconcile.sh` every hour: rediscover accounts/VMs, re-run the idempotent
`enroll.yml` for everything, then print the coverage report. It catches what events cannot: VMs that were already running, new disks
on existing VMs, and a stopped agent or drifted config (re-running enrollment repairs them).
```bash
systemctl list-timers reconcile.timer      # when is the next run?
sudo systemctl start reconcile.service     # run it right now
sudo journalctl -u reconcile -e            # what it did / the coverage report
```
Change the schedule with `OnCalendar=` in `scripts/systemd/reconcile.timer` (e.g. `*:0/15` = every 15 minutes).

## Day-to-day: add / remove things
| I want to... | Command |
|---|---|
| See what exists | `./sandbox.py list` |
| Add a **new** AWS account | `./sandbox.py add-account team-b` |
| Adopt an **existing** account | `./sandbox.py add-account team-b --id 123456789012` |
| Add a VM | `./sandbox.py add-vm team-b api` |
| Add 5 Ubuntu VMs, tagged prod | `./sandbox.py add-vm team-b api --count 5 --os ubuntu --env prod` |
| Add a Windows VM | `./sandbox.py add-vm team-b win --os windows --type t3.medium` |
| Test the opt-out tag | `./sandbox.py add-vm team-b quiet --no-monitoring` |
| Small disk (fills fast, good demo) | `./sandbox.py add-vm team-b demo --disk-gb 8` |
| Remove a VM | `./sandbox.py remove-vm team-b api-3` |
| Remove an account's stuff | `./sandbox.py remove-account team-b` |
| Remove AND close the account | `./sandbox.py remove-account team-b --close` |
| Re-sync AWS with the file | `./sandbox.py apply` |

Each add/remove command edits `sandbox.yaml` and applies it. Add `--no-apply` to only edit the file
(handy for batching), then run `./sandbox.py apply`. You can also just edit `sandbox.yaml` by hand:

```yaml
region: us-east-1
alert_email: me@example.com
email_base: me@example.com       # new accounts get me+sandbox-<name>@example.com
accounts:
- name: home
  id: self                       # "self" = the account you run from
  vms:
  - {name: web-1, os: al2023, env: dev}
- name: team-b                   # no id = tool creates a real AWS account
  vms:
  - {name: api-1, os: ubuntu, env: prod, disk_gb: 8}
  - {name: win-1, os: windows, env: prod, type: t3.medium}
  - {name: quiet, os: al2023, monitoring: false}   # tagged Monitoring=disabled
```
VM fields: `name`, `os` (al2023 | ubuntu | windows), `env`, `type`, `disk_gb`, `monitoring: false`,
`tags: {k: v}`. Then `./sandbox.py apply`. Removing a line and re-applying deletes that VM.
(The commands rewrite the file, so comments in it are not kept - this README is where the docs live.)
After adding an **account**, run `./sandbox.py push-repo` again so the controller's inventory knows about it (new **VMs** in a known account are picked up by the worker automatically).

## What each account gets
* `disk-mon-member-global` : `AnsibleOpsRole` (+ SSM default management role)
* `disk-mon-member-regional`: SSM file-transfer bucket, SSM setting, an EventBridge rule (+role) that forwards "VM started"
  events to the ops account, and the metrics link to the ops account (link skipped for `home`: an account cannot link to itself)
* `sandbox-workloads`: tiny VPC, an instance role (SSM + `CloudWatchAgentServerPolicy`) and your VMs

## Clean up (do this - VMs and the controller cost money)
```bash
./sandbox.py destroy                       # deletes everything this tool created
./sandbox.py destroy --close-accounts      # ...and closes accounts it created
```
Ansible-created alarms (`DiskUsage-*`) are deleted too. The Organization itself is left alone.

## Things to know
**About accounts (read this before `add-account`)**
* `add-account NAME` asks AWS Organizations to create a **brand-new, real AWS account** with its own 12-digit id, its own
  login email and its own resources - it is not a folder or a simulation. It uses `email_base` with `+` addressing
  (`you+sandbox-team-b@gmail.com`); every account needs a unique email.
* **Limit 1 - quota:** an Organization can only hold a limited number of accounts. New organizations often start
  around 10 (check *Service Quotas* -> Organizations). Creating accounts freely will hit it.
* **Limit 2 - closing is slow and rate-limited:** you cannot simply delete an account. `remove-account --close` *closes* it:
  it stays suspended for about 90 days (and may still count toward your quota during that time), and AWS limits how many
  accounts you may close per 30 days. So do not create/close accounts in a loop.
* Without `--close`, `remove-account` deletes everything the tool built in that account and leaves the empty account
  in your Organization. Empty accounts cost nothing.
* Practical advice: **do 90 % of your testing with only the `home` account** (the default). Add a real second account
  once, to prove cross-account access, and reuse it (`add-vm` / `remove-vm`) instead of recreating it.
* **"Throw-away AWS account"** means the account whose credentials you run the tool with (it becomes the management
  account of the Organization and gets IAM roles, an Organization, EC2 instances...). Use a practice account, not your
  company's or your main personal one.

**Other**
* Single region per sandbox (`region:`). Templates use the first availability zone; if an instance type is unavailable
  there, pick another `type`.
* Cost while running: roughly one t3.small controller + one t3.micro per VM (Windows: use t3.medium). Run `destroy` when done.
* `--dry-run` works on every command; use it before the first real run of anything.
* State (org id, account ids, controller id) is in `.sandbox-state.json`; do not delete it while things exist.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `init`: "not in an AWS Organization" | run `./sandbox.py init --create-org` |
| `init`: "MANAGEMENT account" | you are using a member account's credentials |
| `apply`: "Cannot assume OrganizationAccountAccessRole" | new account still warming up (it retries ~3 min), or adopted account uses a different role: `--role-name` |
| VM never appears in `ansible-inventory` | tag `Monitoring=disabled`, VM not running, or role hop failing: run the `sts get-caller-identity --profile` check |
| `ansible ... ping` times out | VM not Online in SSM yet (wait a few minutes; `aws ssm describe-instance-information`) |
| New VM never gets enrolled | `sudo systemctl status enroll-worker` on the controller; `aws sqs get-queue-attributes --queue-url <url> --attribute-names All`; look in the DLQ |
| `push-repo` retries forever | controller still booting or the SSM plugin/agent is not up; check the EC2 console |
