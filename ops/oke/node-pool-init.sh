#!/bin/bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# Initialization script (cloud-init user_data) for the demo's OKE node pool. It
# grows the root filesystem to the boot volume size on every node the pool
# creates, then runs the default OKE logic unchanged. The three commands are
# Oracle's "Using custom cloud-init scripts", example 5, verbatim:
# https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengusingcustomcloudinitscripts.htm
# The first and last commands are the default OKE logic. Do not modify them.
#
# There is deliberately no `set -e`, as in Oracle's example. If oci-growfs fails
# or is missing, cloud-init logs the error and the OKE init script still runs, so
# the node registers with the default-sized root filesystem instead of not at all.
# The pool has one node, so a node that never registers takes the demo down. The
# script's exit status is the OKE init script's.
#
# Nothing in this repository applies this file. Setting it on the node pool is a
# separately approved operator action: docs/node-pool.md.

curl --fail -H "Authorization: Bearer Oracle" -L0 http://169.254.169.254/opc/v2/instance/metadata/oke_init_script | base64 --decode >/var/run/oke-init.sh
bash /usr/libexec/oci-growfs -y
bash /var/run/oke-init.sh
