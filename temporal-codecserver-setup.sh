#!/bin/bash
#
# Clone and build MAAS codec server for Temporal

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GIT_DIR=$SCRIPT_DIR/temporalio-maas-codecserver

sudo apt install -y golang

git clone https://git.launchpad.net/~maas-committers/maas/+git/temporalio-maas-codecserver $GIT_DIR

go build -C $GIT_DIR -o codecserver
mv $GIT_DIR/codecserver $SCRIPT_DIR/codecserver

rm -rf $GIT_DIR
