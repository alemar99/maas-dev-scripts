#!/bin/bash
#
# Clone and build the MAAS codec server for Temporal workflow encryption.
# Runs inside a container (via lxc exec).
# Requires Go; produces a 'codecserver' binary in the script directory.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GIT_DIR="$SCRIPT_DIR"/temporalio-maas-codecserver

sudo apt install -y golang

git clone https://git.launchpad.net/~maas-committers/maas/+git/temporalio-maas-codecserver "$GIT_DIR"

go build -C "$GIT_DIR" -o codecserver
mv "$GIT_DIR"/codecserver "$SCRIPT_DIR"/codecserver

rm -rf "$GIT_DIR"
