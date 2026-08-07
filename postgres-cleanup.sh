#!/bin/bash
#
# Drop and re-create the maasdb PostgreSQL database.
# Runs inside a container (via lxc exec).
set -e

sudo -u postgres dropdb maasdb
sudo -u postgres createdb -O maas maasdb
