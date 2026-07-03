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
# Create a dedicated Python 3.11 virtualenv for NetSquid.
#
# Why a separate env: NetSquid only supports Python 3.10/3.11, while SeQUeNCe 1.0
# needs >=3.12 — they cannot share an interpreter. If your analysis/notebook env
# is 3.12 (so SeQUeNCe runs in-process), NetSquid runs here and is invoked by
# validation.compare as a subprocess via QFABRIC_NETSQUID_PYTHON.
#
# NetSquid is on a private index — set your netsquid.org credentials first:
#   export NETSQUID_USER=... NETSQUID_PASS=...
#
# Usage:
#   bash scripts/setup_netsquid_env.sh
#   PYTHON311=/path/to/python3.11 bash scripts/setup_netsquid_env.sh   # custom interpreter

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PROJECT_DIR}/.venv-netsquid"
PY311="${PYTHON311:-python3.11}"

if ! command -v "${PY311}" >/dev/null 2>&1; then
    echo "ERROR: ${PY311} not found. Install Python 3.11 or set PYTHON311=/path/to/python3.11" >&2
    exit 1
fi

echo "=== Creating NetSquid env at ${VENV} (using ${PY311}) ==="
"${PY311}" -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet numpy pyyaml

if [ -n "${NETSQUID_USER:-}" ] && [ -n "${NETSQUID_PASS:-}" ] \
   && [ "${NETSQUID_USER}" != "..." ] && [ "${NETSQUID_PASS}" != "..." ]; then
    echo "Installing NetSquid from its private index..."
    # Percent-encode: a password with @ : / # (URL) or $ ` ' ! (shell) chars
    # otherwise corrupts the index URL and yields a 401. pip decodes it back.
    ENC_USER="$("${VENV}/bin/python" -c 'import os,urllib.parse as u;print(u.quote(os.environ["NETSQUID_USER"],safe=""))')"
    ENC_PASS="$("${VENV}/bin/python" -c 'import os,urllib.parse as u;print(u.quote(os.environ["NETSQUID_PASS"],safe=""))')"
    "${VENV}/bin/pip" install \
        --extra-index-url "https://${ENC_USER}:${ENC_PASS}@pypi.netsquid.org" \
        netsquid
    echo "=== Done. NetSquid $(${VENV}/bin/python -c 'import netsquid; print(netsquid.__version__)') installed ==="
else
    echo
    echo "NETSQUID_USER / NETSQUID_PASS not set — env created without NetSquid."
    echo "Set your netsquid.org credentials and install into it:"
    echo "    ${VENV}/bin/pip install --extra-index-url \\"
    echo "        \"https://\${NETSQUID_USER}:\${NETSQUID_PASS}@pypi.netsquid.org\" netsquid"
fi

echo
echo "Enable it for cross-validation (run in your primary env):"
echo
echo "    export QFABRIC_NETSQUID_PYTHON=\"${VENV}/bin/python\""
echo
echo "Then: python -m validation.compare validation/scenarios/baseline_1km.yml"
