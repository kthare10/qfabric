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
# Create a dedicated Python 3.12 virtualenv for SeQUeNCe 1.0.
#
# Why a separate env: SeQUeNCe 1.0 requires Python >=3.12, while NetSquid only
# supports 3.10/3.11 — they cannot share an interpreter. QFabric (pure Python)
# and NetSquid run in your primary 3.11 environment; SeQUeNCe runs here and is
# invoked by validation.compare as a subprocess.
#
# Usage:
#   bash scripts/setup_sequence_env.sh
#   PYTHON312=/path/to/python3.12 bash scripts/setup_sequence_env.sh   # custom interpreter
#
# Then, in your primary (3.11) environment before running the cross-validation:
#   export QFABRIC_SEQUENCE_PYTHON="$PWD/.venv-sequence/bin/python"

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PROJECT_DIR}/.venv-sequence"
PY312="${PYTHON312:-python3.12}"

if ! command -v "${PY312}" >/dev/null 2>&1; then
    echo "ERROR: ${PY312} not found. Install Python 3.12 or set PYTHON312=/path/to/python3.12" >&2
    exit 1
fi

echo "=== Creating SeQUeNCe env at ${VENV} (using ${PY312}) ==="
"${PY312}" -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
# numpy + pyyaml are needed by the qfabric validation modules (imported via PYTHONPATH);
# sequence is the simulator itself.
"${VENV}/bin/pip" install --quiet numpy pyyaml "sequence==1.0.0"

echo
echo "=== Done. SeQUeNCe $(${VENV}/bin/python -c 'import sequence; print(sequence.__version__)') installed ==="
echo
echo "Enable it for cross-validation (run in your primary 3.11 env):"
echo
echo "    export QFABRIC_SEQUENCE_PYTHON=\"${VENV}/bin/python\""
echo
echo "Then: python -m validation.compare validation/scenarios/baseline_1km.yml"
echo
echo "NetSquid stays in your primary (3.11) env:  pip install netsquid"
