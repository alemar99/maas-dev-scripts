#!/bin/bash
#
# Configure RBAC authentication for MAAS.
# Runs inside a container (via lxc exec).
# Sources base_candid_rbac_setup.sh for shared setup steps.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR"/base_candid_rbac_setup.sh

IP="$(hostname -I | cut -d' ' -f1)"
RBAC_URL="http://$IP:5000"

sudo cp /var/snap/candid/current/admin.keys /root/
sudo canonical-rbac create-candid-agent /root/admin.keys --service-agent-file /root/rbac.agent
sudo canonical-rbac config --candid-agent-file /root/rbac.agent
sudo canonical-rbac config --service-url "$RBAC_URL"

echo "########## MANUAL STEPS REQUIRED ##########"
echo "RBAC configuration requires some manual steps to be done:"
echo "Run:"
echo "sudo canonical-rbac create-admin"
echo "Admin credentials are admin:maas"
echo "Then visit $RBAC_URL and configure a MAAS service. Give it a name (e.g. maas) and under Groups, write admin"
echo "Finally, add the rbac service to MAAS:"
echo "sudo maas configauth --rbac-url $RBAC_URL --rbac-service-name <service_name>"
