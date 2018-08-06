#!/bin/bash

# check arguments
if [ $# -ne 2 ]; then
    echo "usage: mavlog.sh linkfile logdir"
    exit 2
fi

LOG_DIR=$2
LINK_IMAGE=$1

cd ${LOG_DIR}
#All outputs to Stephen's and Tridge's laptop via Zerotier
mavproxy.py --master=/dev/serial0 --baud=115200 --out=udpout:172.27.131.215:14650 --out=udpout:172.27.234.170:14650 --load-module=cuav.modules.camera_air --mav20 --cmd="set moddebug 3; camera set gcs_address 172.27.131.215:14670:14680:600000,172.27.234.170:14670:14680:60000; camera set camparms /home/pi/cuav/cuav/data/PiCamV2/params.json; camera set imagefile ${LINK_IMAGE}; camera set minscore 1000; camera start;"


