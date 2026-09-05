#!/usr/bin/env bash
# Base 規範 #2 — 受保護檔案完整性
# 比對 .github/policy/manifest.sha256。清單由 Lab 作者以 gen-manifest.sh 產生。
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
. .github/tests/lib.sh

MAN=.github/policy/manifest.sha256
if [ ! -f "$MAN" ]; then
  ok "此 Lab 未宣告受保護檔案（無 manifest.sha256），略過完整性檢查"
  summary; exit $?
fi

MISSING=0
while read -r _hash path; do
  [ -z "${path:-}" ] && continue
  if [ ! -f "$path" ]; then fail "受保護檔案被刪除: $path"; MISSING=1; fi
done < "$MAN"

OUT=$(sha256sum -c "$MAN" 2>&1) || true
BADS=$(printf '%s\n' "$OUT" | grep ': FAILED$' | sed 's/: FAILED$//' || true)
if [ -z "$BADS" ] && [ "$MISSING" -eq 0 ]; then
  ok "受保護檔案未被修改（$(grep -c . "$MAN") 個）"
else
  [ -n "$BADS" ] && { fail "受保護檔案被修改:"; printf '        %s\n' $BADS; }
fi

summary
