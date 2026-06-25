#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Prepare the switch node to run BMv2 from a PREBUILT Docker image instead of
# compiling BMv2/p4c from source — installs Docker and pulls the image.
#
# Run this on the switch node in place of install_bmv2.sh, then enable the Docker
# path in the deployer by exporting (in your JupyterHub kernel):
#     export QFABRIC_BMV2_IMAGE=ghcr.io/kthare10/qfabric-bmv2:latest
#
# Usage:
#   bash scripts/setup_switch_docker.sh [IMAGE]
#   IMAGE default: ghcr.io/kthare10/qfabric-bmv2:latest

set -euo pipefail

IMAGE="${1:-ghcr.io/kthare10/qfabric-bmv2:latest}"

echo "=== QFabric: preparing switch to run BMv2 from Docker image ==="
echo "  Image: ${IMAGE}"

if ! command -v docker >/dev/null 2>&1; then
    echo "--- Installing Docker engine ---"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER" 2>/dev/null || true
else
    echo "--- Docker already installed ---"
fi

echo "--- Pulling ${IMAGE} (one-time; cached thereafter) ---"
# Public GHCR images need no login. For a private image, log in first:
#   echo "$GHCR_TOKEN" | sudo docker login ghcr.io -u <user> --password-stdin
sudo docker pull "${IMAGE}"

echo "--- Verifying the image has the BMv2 toolchain ---"
sudo docker run --rm "${IMAGE}" sh -c \
    "simple_switch --version 2>/dev/null | head -1; p4c-bm2-ss --version 2>/dev/null | head -1"

echo
echo "=== Done. Enable the Docker path in the deployer with: ==="
echo "    export QFABRIC_BMV2_IMAGE='${IMAGE}'"
