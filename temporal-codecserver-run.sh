#!/bin/bash
#
# Run the MAAS Temporal codec server on port 8090.
# Runs inside a container (via lxc exec).
# Requires a 'codecserver' binary built by temporal-codecserver-setup.sh.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

$SCRIPT_DIR/codecserver --key $(sudo cat /var/snap/maas/common/maas/secret) --port 8090

