#!/bin/bash
#
# Build a local simplestreams mirror

# =============== Download images
sudo apt-get update
sudo apt -y install simplestreams
sudo mkdir -p /var/spool/sstreams/maas

# Download image script
cat << EOF | sudo tee /usr/local/bin/sstreams-update
workdir=/var/spool/sstreams/maas
# (jammy|noble) only amd64, edit bellow needed
sstream-mirror --keyring=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg --progress --max=1  https://images.maas.io/ephemeral-v3/daily/ \$workdir 'arch=amd64' 'release~(jammy|noble)'
sstream-mirror --keyring=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg --progress --max=1  https://images.maas.io/ephemeral-v3/daily/ \$workdir 'os~(grub*|pxelinux)'
EOF

# Manually run once
sudo chmod +x /usr/local/bin/sstreams-update
/usr/local/bin/sstreams-update

# Update images daily
echo "/usr/local/bin/sstreams-update" | sudo tee -a /etc/cron.daily/mirror-update

# =============== Serve mirror
PORT=8001 # Port 80 and 8000 in use by MAAS
sudo apt -y install apache2
sudo sed -i "s/Listen 80$/Listen $PORT/" /etc/apache2/ports.conf
sudo systemctl restart apache2

cat << EOF | sudo tee /etc/apache2/sites-available/sstreams-mirror.conf
<VirtualHost *:$PORT>
    DocumentRoot /var/spool/sstreams/maas

    LogLevel info
    ErrorLog /var/log/apache2/maas.mirror-error.log
    CustomLog /var/log/apache2/maas.mirror-access.log combined
    <Directory /var/spool/sstreams/>
     Options Indexes FollowSymLinks
     AllowOverride None
     Require all granted
    </Directory>
</VirtualHost>
EOF

sudo systemctl reload apache2
sudo systemctl restart apache2
sudo a2ensite sstreams-mirror.conf
