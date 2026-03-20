#!/bin/bash
#
# Copy mTLS certificates from MAAS to temporal-ui snap

sudo cp /var/snap/maas/current/certificates/* /var/snap/temporal-ui/common/maas-certificates/

sudo snap restart temporal-ui
