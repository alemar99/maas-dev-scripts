#!/bin/bash
#
# Base script to enable Candid or RBAC in MAAS.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default values
MAAS_PW="maas"
HOST_IP=$(hostname -I | cut -d" " -f1)
CANDID_URL="http://$HOST_IP:8081"
RBAC_URL="http://$HOST_IP:5000"

# Candid + RBAC setup
PGVER=$(ls -1 /etc/postgresql | tail -1)
sudo ln -sf "$PGVER" /etc/postgresql/10
sudo dpkg -i $SCRIPT_DIR/rbac_stable.deb

# Update pg_hba.conf to allow candid/rbac access to maasdb
hba_file="/etc/postgresql/$PGVER/main/pg_hba.conf"
sudo sed -E -i -e '/(candid|rbac)/d' "$hba_file"
sudo grep maasdb "$hba_file" | sort -u | sed -E -e 's/maas(db)?/candid/g' | sudo tee -a "$hba_file"
sudo grep maasdb "$hba_file" | sort -u | sed -E -e 's/maas(db)?/rbac/g' | sudo tee -a "$hba_file"

cluster=$(pg_conftool show cluster_name | awk -F\' '{print $2}')
sudo -u postgres pg_ctlcluster "${cluster%/*}" "${cluster#*/}" reload
sudo snap restart candid

# Modify Candid config
sudo sed -E -i -e \
    "s/(^location:[^/]*..)[^:]*(.*)/\1$HOST_IP\2/; \
     s/(password: ).*/\1$MAAS_PW/; \
     s/group1/admin/; \
     /group2/d" \
    /var/snap/candid/current/config.yaml

sudo sed -E -i -e \
    "s/(.*url[^/]*..)[^:]*(.*)/\1$HOST_IP\2/" \
    /var/snap/candid/current/admin.keys

sudo snap restart candid
