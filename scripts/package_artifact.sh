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
# Build a clean .tgz of QFabric suitable for upload to the FABRIC Artifact
# Manager (https://artifacts.fabric-testbed.net). Excludes virtualenvs, VCS
# metadata, caches, build artifacts, and personal/session files.
#
# Usage:
#   bash scripts/package_artifact.sh [version]
# Example:
#   bash scripts/package_artifact.sh v0.1.0   ->  dist/qfabric-v0.1.0.tgz

set -euo pipefail

# Stop macOS bsdtar from emitting AppleDouble (._*) resource-fork files.
export COPYFILE_DISABLE=1

VERSION="${1:-v0.1.0}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="qfabric"
OUT_DIR="${PROJECT_DIR}/dist"
TARBALL="${OUT_DIR}/${PROJECT_NAME}-${VERSION}.tgz"

mkdir -p "${OUT_DIR}"

EXCLUDES=(
  --exclude='.git' --exclude='.github'
  --exclude='.venv' --exclude='venv' --exclude='.venv-*'
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache'
  --exclude='*.egg-info' --exclude='dist' --exclude='build'
  --exclude='.idea' --exclude='.vscode' --exclude='.DS_Store' --exclude='.ipynb_checkpoints'
  --exclude='._*' --exclude='cc-usage-log.md' --exclude='*.log' --exclude='*.pcap'
  --exclude='results/cross_validation.json' --exclude='results/all_scenarios.json'
  --exclude='results/_tmp_scenarios' --exclude='p4/bmv2/*.json'
)

# Stage a filtered copy, strip Jupyter notebook outputs there (so the artifact
# ships clean templates regardless of what's been executed locally), then tar the
# staged copy. The working repo is left untouched.
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

tar "${EXCLUDES[@]}" -cf - -C "$(dirname "${PROJECT_DIR}")" "$(basename "${PROJECT_DIR}")" \
  | tar -xf - -C "${STAGE}"

python3 - "${STAGE}" <<'PY'
import sys, json, glob, os
count = 0
for f in glob.glob(os.path.join(sys.argv[1], "*", "notebooks", "*.ipynb")):
    nb = json.load(open(f))
    for c in nb.get("cells", []):
        if c.get("cell_type") == "code":
            c["outputs"] = []
            c["execution_count"] = None
    json.dump(nb, open(f, "w"), indent=1)
    count += 1
print(f"  stripped outputs from {count} notebooks")
PY

tar -czf "${TARBALL}" -C "${STAGE}" "$(basename "${PROJECT_DIR}")"

echo "Built artifact: ${TARBALL}"
echo "Size: $(du -h "${TARBALL}" | cut -f1)"
echo
echo "Contents (top level):"
tar -tzf "${TARBALL}" | sed "s|^$(basename "${PROJECT_DIR}")/||" | awk -F/ 'NF<=2' | sort -u | head -40
