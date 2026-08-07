#!/bin/bash
#
# OIDC setup, add the missing env vars in oidc_vars.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source $SCRIPT_DIR/oidc_vars.sh

HOST_IP="$(hostname -I | cut -d' ' -f1)"

maas admin oidc-providers create -k \
    name=auth0 \
    issuer_url="$ISSUER_URL" \
    client_id="$CLIENT_ID" \
    client_secret="$CLIENT_SECRET" \
    enabled=true \
    token_type="Opaque" \
    redirect_uri="http://$HOST_IP:5240/MAAS/r/login/oidc/callback" \
    scopes='openid profile email offline_access'
