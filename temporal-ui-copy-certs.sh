#!/bin/bash
#
# Copy mTLS certificates from MAAS snap to temporal-ui snap.
# Runs inside a container (via lxc exec).
# Run this after MAAS has generated its certificates.
set -e

sudo cp /var/snap/maas/current/certificates/* /var/snap/temporal-ui/common/maas-certificates/

sudo snap restart temporal-ui
