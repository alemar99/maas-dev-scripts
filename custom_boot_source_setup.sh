#!/bin/bash

IP="$(hostname -I | cut -d' ' -f1)"
maas admin boot-sources create url="http://$IP:8001/" name="Local boot source" skip_keyring_verification=true keyring_filename=/snap/maas/current/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg
