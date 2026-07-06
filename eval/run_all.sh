#!/bin/sh
# SPDX-License-Identifier: GPL-2.0

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/daxfs_eval.py" "$@"

