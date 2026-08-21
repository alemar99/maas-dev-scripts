#!/bin/bash
#
# Install MAAS (snap or deb) and initialize region+rack.
# Usage:
#   /scripts/maas-install.sh snap <DB_IP> <MAAS_CHANNEL>
#   /scripts/maas-install.sh deb  <PPA> <BRANCH>
# This is mounted at /scripts inside containers via the LXD profile.
set -e

METHOD="$1"

clone_maas() {
    # $1 = git branch to clone into /work
    sudo git clone --branch "$1" --depth 10 --shallow-submodules \
        https://github.com/canonical/maas /work
    sudo chown -R ubuntu:ubuntu /work
}

case "$METHOD" in
    snap)
        DB_IP="$2"
        MAAS_CHANNEL="$3"
        HOST_IP="$(hostname -I | cut -d' ' -f1)"

        TRACK="${MAAS_CHANNEL%%/*}"
        if [ "$TRACK" = "latest" ]; then
            BRANCH="master"
        else
            BRANCH="$TRACK"
        fi

        clone_maas "$BRANCH"

        sudo snap install maas --channel="$MAAS_CHANNEL"
        /work/utilities/connect-snap-interfaces

        sudo maas init region+rack \
            --database-uri="postgres://maas:maas@${DB_IP}/maasdb" \
            --maas-url="http://$HOST_IP:5240/MAAS"
        ;;
    deb)
        PPA="$2"
        BRANCH="$3"

        clone_maas "$BRANCH"

        sudo add-apt-repository -y "$PPA"
        sudo apt update
        sudo apt install -y maas
        sudo maas init --skip-admin --candid-domain '' --rbac-url ''
        ;;
    *)
        echo "Usage: $0 snap <DB_IP> <MAAS_CHANNEL> | deb <PPA> <BRANCH>" >&2
        exit 1
        ;;
esac
