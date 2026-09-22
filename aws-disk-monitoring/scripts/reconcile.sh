#!/usr/bin/env bash
# Periodic SAFETY NET (not the main mechanism): events can be missed, so once an hour
# rediscover accounts/VMs, re-run the (idempotent) enrollment, and report anything unmonitored.
set -uo pipefail
cd "$(dirname "$0")/.."
rc=0
python3 inventory/generate_inventory.py ${USE_ORG:+--from-org} || rc=1
ansible-playbook playbooks/enroll.yml            || rc=$?
ansible-playbook playbooks/verify_coverage.yml   || rc=$?
exit "$rc"
