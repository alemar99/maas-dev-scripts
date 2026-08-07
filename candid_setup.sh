#!/bin/bash
#
# Configure Candid authentication for MAAS.
# Runs inside a container (via lxc exec).
# Sources base_candid_rbac_setup.sh for shared setup steps.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source $SCRIPT_DIR/base_candid_rbac_setup.sh
