#!/bin/bash


echo "Loading any env vars from .env"
eval "$(
  cat .env | awk '!/^\s*#/' | awk '!/^\s*$/' | while IFS='' read -r line; do
    key=$(echo "$line" | cut -d '=' -f 1)
    value=$(echo "$line" | cut -d '=' -f 2-)
    echo "export $key=\"$value\""
  done
)"


find . -name "*.pyc" -exec rm -f {} \;
export AWS_PROFILE=dev-lagunacreek;
sls offline --stage local --noAuth --httpPort 5500 --reloadHandler;

