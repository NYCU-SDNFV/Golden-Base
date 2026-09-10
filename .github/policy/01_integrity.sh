#!/usr/bin/env bash
# Base 規範 #2 — 受保護檔案完整性
# 比對 .github/policy/manifest.sha256。清單由 Lab 作者以 gen-manifest.sh 產生。
set -uo pipefail
# lib.sh 住在哪裡取決於誰在跑這支腳本：
#   學生 checkout  -> .github/tests/lib.sh
#   Classroom 50 bundle -> 跟本檔同一層（$CLASSROOM50_BUNDLE_DIR/policy/lib.sh）
# 兩邊同一份檔案，靠這個 resolver 決定，不要為了 bundle 另外複製一份改過的腳本。
_HERE=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
if [ -f "$_HERE/lib.sh" ]; then . "$_HERE/lib.sh"; else . .github/tests/lib.sh; fi

# 同理：bundle 版的 manifest 是 canonical 的（學生改不到），
# 學生 checkout 版只給本機 `make policy` 用。
MAN="$_HERE/manifest.sha256"
[ -f "$MAN" ] || MAN=.github/policy/manifest.sha256
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
