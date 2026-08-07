#!/bin/bash
#
# Enables TLS for maas. MAAS url is now https://MAAS_IP:5443

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
