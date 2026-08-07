#!/bin/bash

file_path="$1"
name="$2"
title="$3"
arch="${4:-amd64}"
if [[ -z "$file_path" || -z "$name" || -z "$title" ]]; then
  echo "Usage: $0 file_path os/release title [arch]"
  exit 1
fi

APIKEY="${APIKEY:-$(sudo maas apikey --username maas)}"
IFS=':' read -r CONSUMER_KEY TOKEN SIGNATURE <<< $APIKEY
SIGNATURE="&${SIGNATURE}"
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

CHUNK_SIZE=$((4 * 1024 * 1024))  # 4MB

file_size=$(stat --format="%s" "$file_path")
file_sha256=$(sha256sum "$file_path" | cut -d ' ' -f 1)

response=$(curl -X POST "http://localhost:5240/MAAS/api/2.0/boot-resources/" \
     -H "$(build_auth_header)" \
     -F "size=$file_size" \
     -F "sha256=$file_sha256" \
     -F "name=$name" \
     -F "title=$title" \
     -F "architecture=$arch/generic" \
     -F "filetype=tgz")

upload_uri=$(jq -r '.sets | to_entries | sort_by(.key) | reverse | .[0].value.files | to_entries | .[0].value.upload_uri' <<< "$response")

tmp_dir=$(mktemp -d)

split -b $CHUNK_SIZE "$file_path" "$tmp_dir/chunk_"

for chunk in "$tmp_dir"/chunk_*; do
  response=$(curl -X PUT http://localhost:5240$upload_uri \
       -H "Content-Type: application/octet-stream" \
       -H "Content-Length: $(stat --format="%s" "$chunk")" \
       -H "$(build_auth_header)" \
       --data-binary @"$chunk" \
       --write-out "%{http_code}" --silent --output /dev/null)

  if [[ "$response" -ne 200 ]]; then
      echo "Upload failed with status code $response"
      rm -r "$tmp_dir"
      exit 1
  fi
  rm "$chunk"
done

rm -r "$tmp_dir"

echo "Upload complete!"
