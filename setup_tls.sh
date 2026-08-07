#!/bin/bash
#
# Enable TLS for MAAS snap; the MAAS URL becomes https://MAAS_IP:5443.
# Runs inside a container (via lxc exec).
# Generates a self-signed certificate valid for 365 days.
set -e

sudo mkdir -p /var/snap/maas/common/certs
sudo openssl req -x509 -newkey rsa:4096 \
    -keyout /var/snap/maas/common/certs/server.key \
    -out /var/snap/maas/common/certs/server.crt \
    -subj "/C=IT/ST=Rome/L=Rome/O=MAASive Company/OU=IT/CN=example.com" \
    -sha256 -days 365 -nodes

sudo maas config-tls enable --yes \
   /var/snap/maas/common/certs/server.key \
   /var/snap/maas/common/certs/server.crt

sudo snap restart maas
