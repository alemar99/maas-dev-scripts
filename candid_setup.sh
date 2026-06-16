#!/bin/bash
#
# Candid-specific setup steps

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source $SCRIPT_DIR/base_candid_rbac_setup.sh

sudo cp /var/snap/candid/current/admin.keys /root/
sudo candid -a /root/admin.keys create-agent -f /root/maas.agent
USERNAME=$(sudo cat /root/maas.agent | jq '.agents[0].username')
sudo candid -a /root/admin.keys acl grant read-user-groups "$USERNAME"
sudo candid -a /root/admin.keys acl grant read-user "$USERNAME"
