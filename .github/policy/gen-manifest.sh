#!/usr/bin/env bash
# 【Lab 作者用】產生受保護檔案清單。學生端不需要執行。
#   用法: .github/policy/gen-manifest.sh Makefile Dockerfile docker/entrypoint.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
[ $# -gt 0 ] || { echo "用法: $0 <要保護的檔案...>" >&2; exit 2; }
sha256sum "$@" > .github/policy/manifest.sha256
echo "已寫入 .github/policy/manifest.sha256:"
cat .github/policy/manifest.sha256
