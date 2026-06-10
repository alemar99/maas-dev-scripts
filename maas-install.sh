#!/bin/bash
#
# Install MAAS snap, connect interfaces, and initialize region+rack
# Usage: /scripts/maas-install.sh <DB_IP>
# This is mounted at /scripts inside containers via LXD profile

DB_IP="$1"
MAAS_CHANNEL="$2"

HOST_IP="$(hostname -I | cut -d' ' -f1)"

TRACK="${MAAS_CHANNEL%%/*}"
if [ "$TRACK" = "latest" ]; then
    BRANCH="master"
else
    BRANCH="$TRACK"
fi

sudo git clone --branch "$BRANCH" --depth 10 --shallow-submodules https://github.com/canonical/maas /work
sudo chown -R ubuntu:ubuntu /work

sudo snap install --beta snapd
sudo snap install --beta core26
sudo snap install maas --channel="$MAAS_CHANNEL"
/work/utilities/connect-snap-interfaces

sudo maas init region+rack \
    --database-uri="postgres://maas:maas@${DB_IP}/maasdb" \
    --maas-url="http://$HOST_IP:5240/MAAS"
