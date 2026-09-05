#!/usr/bin/env bash
# Base 規範 #1 — repo 結構與檔名
# 所有 SDNFV Lab 共用。位於 .github/ 之下，學生無法竄改。
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
. .github/tests/lib.sh

# --- 1. 必要檔案 ---
for f in README.md Makefile .gitattributes AGENTS.md CLAUDE.md; do assert_file "$f"; done

# --- 2. 禁止提交的檔案 ---
BANNED=$(git ls-files | grep -Ei '(^|/)(\.env|id_rsa|.*\.pem)$|\.solution\.|LAB[0-9]+-SOLUTION\.md' || true)
if [ -z "$BANNED" ]; then ok "無禁止檔案（解答/金鑰）"
else fail "偵測到禁止檔案:"; printf '%s\n' "$BANNED" | sed 's/^/        /'; fi

# --- 3. 檔名規範：只允許 [A-Za-z0-9._/-] ---
BADNAME=$(git ls-files | grep -Pv '^[A-Za-z0-9._/-]+$' || true)
if [ -z "$BADNAME" ]; then ok "檔名符合規範（無空白/非 ASCII）"
else fail "檔名不合規範:"; printf '%s\n' "$BADNAME" | sed 's/^/        /'; fi

# --- 4. 行尾：不得有 CRLF ---
CRLF=$(git ls-files -z | xargs -0 -r grep -rlI $'\r$' 2>/dev/null || true)
if [ -z "$CRLF" ]; then ok "無 CRLF 行尾"
else fail "含 CRLF（請確認 .gitattributes 生效後重新 commit）:"; printf '        %s\n' $CRLF; fi

# --- 5. 檔案大小：單檔不得 > 5MB ---
BIG=$(git ls-files -z | xargs -0 -r -I{} sh -c '[ -f "{}" ] && [ $(stat -c%s "{}") -gt 5242880 ] && echo "{}"' 2>/dev/null || true)
if [ -z "$BIG" ]; then ok "無過大檔案（>5MB）"
else fail "檔案過大:"; printf '        %s\n' $BIG; fi

summary
