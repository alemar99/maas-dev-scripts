#!/bin/bash
#
# Install and configure temporal-ui snap for MAAS.
# Runs inside a container (via lxc exec).
# Configures temporal-ui to talk to the MAAS Temporal server.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo snap install temporal-ui
sudo cp $SCRIPT_DIR/temporal-ui-config.yaml /var/snap/temporal-ui/common/config/production.yaml

IP=$(hostname -I | cut -d" " -f1)
sudo sed -i -e "s/IP_REPLACE/$IP/" /var/snap/temporal-ui/common/config/production.yaml

sudo mkdir -p /var/snap/temporal-ui/common/maas-certificates

source $SCRIPT_DIR/temporal-ui-copy-certs.sh

