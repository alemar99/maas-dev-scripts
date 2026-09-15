#!/bin/bash
#
# Configure Candid authentication for MAAS.
# Runs inside a container (via lxc exec).
# Sources base_candid_rbac_setup.sh for shared setup steps.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR"/base_candid_rbac_setup.sh

sudo cp /var/snap/candid/current/admin.keys /root/
sudo candid -a /root/admin.keys create-agent -f /root/maas.agent
USERNAME=$(sudo cat /root/maas.agent | jq '.agents[0].username')
sudo candid -a /root/admin.keys acl grant read-user-groups "$USERNAME"
sudo candid -a /root/admin.keys acl grant read-user "$USERNAME"

sudo cp /var/snap/candid/current/admin.keys /tmp/snap-private-tmp/snap.maas/tmp/.
sudo maas configauth --rbac-url '' --candid-agent-file /tmp/admin.keys \
    --candid-domain '' --candid-admin-group admin
