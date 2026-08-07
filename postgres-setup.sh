#!/bin/bash
#
# Install and configure PostgreSQL for MAAS.
# Runs inside a container (via lxc exec).
# Idempotent: safe to run more than once.
set -e

sudo apt update
sudo apt install -y postgresql
sudo -u postgres psql -c "SELECT 'CREATE USER maas WITH ENCRYPTED PASSWORD ''maas''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='maas')\gexec"
sudo -u postgres psql -c "SELECT 'CREATE DATABASE maasdb OWNER maas' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='maasdb')\gexec"
PGVER=$(ls -1 /etc/postgresql | tail -1)
echo -e "\nhost maasdb maas 0.0.0.0/0 md5" | sudo tee -a /etc/postgresql/$PGVER/main/pg_hba.conf

# Multi node setup
echo -e "\nlisten_addresses = '*'" | sudo tee -a /etc/postgresql/$PGVER/main/postgresql.conf

sudo sed -i -e 's/^max_connections = .*/max_connections = 300/' /etc/postgresql/$PGVER/main/postgresql.conf
sudo sed -i -e 's/^shared_buffers = .*/shared_buffers = 80MB/' /etc/postgresql/$PGVER/main/postgresql.conf

sudo systemctl restart postgresql
