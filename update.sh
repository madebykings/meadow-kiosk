#!/bin/bash
set -e
cd /home/meadow/meadow-kiosk
git pull
sudo systemctl restart meadow-kiosk.service
