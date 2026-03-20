#!/bin/bash
#
# Script to query the MAAS v2 API

IP=$(hostname -I | cut -d" " -f1)
MAAS_URL="http://$IP:5240"

APIKEY="${APIKEY:-$(sudo maas apikey --username maas)}"
IFS=':' read -r CONSUMER_KEY TOKEN SIGNATURE <<< $APIKEY
SIGNATURE="&${SIGNATURE}"

ENDPOINT="$1"
shift

build_auth_header() {
  local nonce timestamp
  nonce=$(uuidgen)
  timestamp=$(date +%s)
  echo "Authorization: OAuth \
oauth_version=\"1.0\", \
oauth_signature_method=\"PLAINTEXT\", \
oauth_consumer_key=\"$CONSUMER_KEY\", \
oauth_token=\"$TOKEN\", \
oauth_signature=\"$SIGNATURE\", \
oauth_nonce=\"$nonce\", \
oauth_timestamp=\"$timestamp\""
}

curl --header "$(build_auth_header)" "$MAAS_URL/MAAS/api/2.0/$ENDPOINT/" "$@"
