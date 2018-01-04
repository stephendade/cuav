#!/bin/bash

# check arguments
if [ $# -ne 1 ]; then
    echo "usage: mavlog.sh logdir"
    exit 1
fi

LOG_DIR=$1

cd ${LOG_DIR}
mavproxy.py

