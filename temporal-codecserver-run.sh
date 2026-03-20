#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

$SCRIPT_DIR/codecserver --key $(sudo cat /var/snap/maas/common/maas/secret) --port 8090

