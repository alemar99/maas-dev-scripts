#!/bin/bash
#
# Setup a 3-node MAAS environment with LXD containers

LXD_NETWORK="maas-ha-net"

lxc network create $LXD_NETWORK

PREFIX=maas-ha-node

CONTAINERS="$PREFIX-1 $PREFIX-2 $PREFIX-3"

exec_all() {
    printf '%s\n' $CONTAINERS | xargs -P0 -I{} sh -c "$*"
}

lxc_exec_all() {
    printf '%s\n' $CONTAINERS | xargs -P0 -I{} lxc exec {} -- sh -c "$*"
}

PROFILE=./lxd-maas-profile.yaml
# Launch 3 containers
exec_all "lxc init ubuntu:24.04 {} < $PROFILE"
# Or if you use the maas-dev profile
# exec_all "lxc init ubuntu:24.04 {} -p default -p maas-dev"
exec_all "lxc config device add {} eth0 nic network=$LXD_NETWORK"
exec_all "lxc start {}"

# Install postgres on node 1
lxc exec "$PREFIX-1" -- sh -c "/work/dev-scripts/postgres-setup.sh"
DB_IP=$(lxc exec "$PREFIX-1" -- sh -c "hostname -I | cut -d' ' -f1")

# Install and init maas on all nodes
INSTALL_CMD="sudo snap install maas --channel=latest/edge"
lxc_exec_all $INSTALL_CMD
lxc_exec_all "/work/utilities/connect-snap-interfaces"
INIT_CMD="sudo maas init region+rack --database-uri=postgres://maas:maas@$DB_IP/maasdb"
INIT_CMD+=' --maas-url=http://$(hostname -I | cut -d" " -f1):5240/MAAS'

for c in $CONTAINERS; do lxc exec "$c" -- sh -c  "$INIT_CMD"; done;

lxc exec "$PREFIX-1" -- sh -c "sudo maas createadmin --username maas --password maas --email maas@admin"
lxc_exec_all 'maas login admin http://$(hostname -I | cut -d" " -f1):5240/MAAS/api/2.0 $(sudo maas apikey --username maas)'
