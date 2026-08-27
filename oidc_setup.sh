#!/bin/bash
#
# Configure OIDC authentication provider for MAAS.
# Runs inside a container (via lxc exec).
# Requires oidc_vars.sh to be populated with ISSUER_URL, CLIENT_ID, CLIENT_SECRET.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR"/oidc_vars.sh

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
