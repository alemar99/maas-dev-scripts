#!/bin/bash
#
# Drop and re-create the maasdb

sudo -u postgres dropdb maasdb
sudo -u postgres createdb -O maas maasdb
