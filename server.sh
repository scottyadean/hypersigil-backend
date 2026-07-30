#!/bin/bash
find . -name "*.pyc" -exec rm -f {} \;
export AWS_PROFILE=dev-lagunacreek;
sls offline --stage local --noAuth --httpPort 5500 --reloadHandler;

