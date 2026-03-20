#!/bin/bash
#
# Setup PostgreSQL for MAAS

sudo apt update
sudo apt install -y postgresql
sudo -u postgres psql -c "create user maas with encrypted password 'maas'"
sudo -u postgres createdb -O maas maasdb
PGVER=$(ls -1 /etc/postgresql | tail -1)
echo -e "\nhost maasdb maas 0.0.0.0/0 md5" | sudo tee -a /etc/postgresql/$PGVER/main/pg_hba.conf

# Multi node setup
echo -e "\nlisten_addresses = '*'" | sudo tee -a /etc/postgresql/$PGVER/main/postgresql.conf

sudo sed -i -e 's/^max_connections = .*/max_connections = 300/' /etc/postgresql/$PGVER/main/postgresql.conf
sudo sed -i -e 's/^shared_buffers = .*/shared_buffers = 80MB/' /etc/postgresql/$PGVER/main/postgresql.conf

sudo systemctl restart postgresql
