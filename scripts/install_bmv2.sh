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

# install_bmv2.sh — Install P4 compiler (p4c) and BMv2 software switch.
# Supports Rocky 9 / AlmaLinux 9 and Ubuntu 22.04+.
set -euo pipefail

echo "=== QFabric: Installing BMv2 and p4c ==="

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID}"
else
    echo "ERROR: Cannot detect OS" >&2
    exit 1
fi

install_ubuntu() {
    echo "--- Detected Ubuntu/Debian ---"
    sudo apt-get update
    sudo apt-get install -y \
        automake cmake g++ git libtool python3 python3-pip \
        libboost-all-dev libgc-dev bison flex libfl-dev \
        libgmp-dev libpcap-dev pkg-config tcpdump

    # Install PI (P4Runtime dependency)
    if ! command -v simple_switch &>/dev/null; then
        echo "--- Installing behavioral-model (BMv2) ---"
        cd /tmp
        if [ ! -d behavioral-model ]; then
            git clone --depth 1 https://github.com/p4lang/behavioral-model.git
        fi
        cd behavioral-model
        ./install_deps.sh
        ./autogen.sh
        ./configure --enable-debugger
        make -j"$(nproc)"
        sudo make install
        sudo ldconfig
    else
        echo "--- BMv2 already installed ---"
    fi

    # Install p4c
    if ! command -v p4c &>/dev/null; then
        echo "--- Installing p4c ---"
        cd /tmp
        if [ ! -d p4c ]; then
            git clone --recursive --depth 1 https://github.com/p4lang/p4c.git
        fi
        cd p4c
        mkdir -p build && cd build
        cmake .. -DCMAKE_BUILD_TYPE=Release
        make -j"$(nproc)"
        sudo make install
    else
        echo "--- p4c already installed ---"
    fi

    # Install PTF for data plane testing
    pip3 install --user ptf scapy
}

install_rocky() {
    echo "--- Detected Rocky/AlmaLinux ---"
    sudo dnf install -y epel-release
    sudo dnf config-manager --set-enabled crb 2>/dev/null || \
        sudo dnf config-manager --set-enabled powertools 2>/dev/null || true
    sudo dnf groupinstall -y "Development Tools"
    sudo dnf install -y \
        automake cmake gcc-c++ git libtool python3 python3-pip python3-devel \
        boost-devel bison flex gmp-devel jsoncpp-devel \
        pkgconf tcpdump libpcap-devel gc-devel

    # BMv2 depends on thrift and nanomsg, which install_deps.sh handles for
    # Ubuntu only. On Rocky we must build them manually.

    # --- Install Apache Thrift (BMv2 dependency) ---
    if ! pkg-config --exists thrift 2>/dev/null; then
        echo "--- Installing Apache Thrift ---"
        sudo dnf install -y openssl-devel libevent-devel zlib-devel boost-static
        cd /tmp
        if [ ! -d thrift-0.13.0 ]; then
            curl -LO https://archive.apache.org/dist/thrift/0.13.0/thrift-0.13.0.tar.gz
            tar xzf thrift-0.13.0.tar.gz
        fi
        cd thrift-0.13.0
        ./configure --without-java --without-nodejs --without-go \
            --without-ruby --without-erlang --without-perl \
            --without-php --without-csharp --without-dotnetcore \
            --without-haskell --without-lua --without-rs --without-swift \
            --without-d --without-dart --without-haxe --without-netstd \
            --with-cpp --with-python
        make -j"$(nproc)"
        sudo make install
        sudo ldconfig
    else
        echo "--- Thrift already installed ---"
    fi

    # Ensure /usr/local/lib and /usr/local/lib64 are in ldconfig
    echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/usr-local.conf > /dev/null
    echo "/usr/local/lib64" | sudo tee -a /etc/ld.so.conf.d/usr-local.conf > /dev/null
    sudo ldconfig

    # --- Install nanomsg (BMv2 dependency) ---
    if ! pkg-config --exists nanomsg 2>/dev/null && ! [ -f /usr/local/lib/libnanomsg.so ] && ! [ -f /usr/local/lib64/libnanomsg.so ]; then
        echo "--- Installing nanomsg ---"
        cd /tmp
        if [ ! -d nanomsg ]; then
            git clone --depth 1 https://github.com/nanomsg/nanomsg.git
        fi
        cd nanomsg
        mkdir -p build && cd build
        cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local
        make -j"$(nproc)"
        sudo make install
        sudo ldconfig
    else
        echo "--- nanomsg already installed ---"
    fi

    # --- Install xxhash (BMv2 dependency) ---
    if ! pkg-config --exists libxxhash 2>/dev/null && ! [ -f /usr/local/lib/libxxhash.so ]; then
        echo "--- Installing xxhash ---"
        cd /tmp
        if [ ! -d xxHash ]; then
            git clone --depth 1 https://github.com/Cyan4973/xxHash.git
        fi
        cd xxHash
        make -j"$(nproc)"
        sudo make install PREFIX=/usr/local
        sudo ldconfig
    else
        echo "--- xxhash already installed ---"
    fi

    # --- Build BMv2 ---
    if ! command -v simple_switch &>/dev/null; then
        echo "--- Installing behavioral-model (BMv2) ---"
        cd /tmp
        if [ ! -d behavioral-model ]; then
            git clone --depth 1 https://github.com/p4lang/behavioral-model.git
        fi
        cd behavioral-model
        # Skip install_deps.sh (Ubuntu-only), deps already installed above
        ./autogen.sh
        ./configure --enable-debugger \
            --with-thrift \
            --with-nanomsg \
            CPPFLAGS="-I/usr/local/include" \
            LDFLAGS="-L/usr/local/lib"
        make -j"$(nproc)"
        sudo make install
        sudo ldconfig
    else
        echo "--- BMv2 already installed ---"
    fi

    # --- Build p4c ---
    if ! command -v p4c &>/dev/null; then
        echo "--- Installing p4c ---"
        sudo dnf install -y bzip2-devel
        cd /tmp
        if [ ! -d p4c ]; then
            git clone --recursive --depth 1 https://github.com/p4lang/p4c.git
        fi
        cd p4c
        mkdir -p build && cd build
        cmake .. -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_INSTALL_PREFIX=/usr/local \
            -DENABLE_BMV2=ON \
            -DENABLE_EBPF=OFF \
            -DENABLE_UBPF=OFF \
            -DENABLE_DPDK=OFF \
            -DENABLE_P4TC=OFF \
            -DENABLE_GTESTS=OFF
        make -j"$(nproc)"
        sudo make install
    else
        echo "--- p4c already installed ---"
    fi

    pip3 install --user ptf scapy
}

case "${OS_ID}" in
    ubuntu|debian)
        install_ubuntu
        ;;
    rocky|almalinux|centos|rhel)
        install_rocky
        ;;
    *)
        echo "ERROR: Unsupported OS '${OS_ID}'. Use Ubuntu or Rocky Linux." >&2
        exit 1
        ;;
esac

echo "=== BMv2 and p4c installation complete ==="
echo "Verify: simple_switch --version && p4c --version"
