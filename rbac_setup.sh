#!/bin/bash
#
# RBAC-specific setup steps

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source $SCRIPT_DIR/base_candid_rbac_setup.sh

sudo cp /var/snap/candid/current/admin.keys /root/
sudo canonical-rbac create-candid-agent /root/admin.keys --service-agent-file /root/rbac.agent
sudo canonical-rbac config --candid-agent-file /root/rbac.agent
sudo canonical-rbac config --service-url "$RBAC_URL"
sudo canonical-rbac create-admin

echo "Visit $RBAC_URL and configure a MAAS service then run:"
echo "sudo maas configauth --rbac-url $RBAC_URL --rbac-service-name <maas_service>"
