#!/usr/bin/env bash
# SDNFV Golden Base — 共用 test helper
# 位於 .github/ 之下：每次 gh student submit 會從 template 重新抓取，學生無法竄改。
set -uo pipefail

_PASS=0; _FAIL=0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  _G=$'\033[32m'; _R=$'\033[31m'; _Y=$'\033[33m'; _N=$'\033[0m'
else
  _G=""; _R=""; _Y=""; _N=""
fi

ok()   { _PASS=$((_PASS+1)); printf '%s  PASS%s  %s\n' "$_G" "$_N" "$*"; }
fail() { _FAIL=$((_FAIL+1)); printf '%s  FAIL%s  %s\n' "$_R" "$_N" "$*"; }
info() { printf '%s  ..  %s%s\n' "$_Y" "$*" "$_N"; }

# assert_cmd <描述> <指令...>
assert_cmd() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail "$desc  (\$*: $*)"; fi
}

# assert_file <路徑>
assert_file() {
  if [ -f "$1" ]; then ok "檔案存在: $1"; else fail "檔案缺少: $1"; fi
}

# assert_absent <路徑>
assert_absent() {
  if [ ! -e "$1" ]; then ok "未提交禁止檔案: $1"; else fail "不應存在: $1"; fi
}

# assert_match <描述> <regex> <字串>
assert_match() {
  local desc="$1" re="$2" s="$3"
  if printf '%s' "$s" | grep -Eq "$re"; then ok "$desc"; else fail "$desc  (期望符合 /$re/)"; fi
}

summary() {
  printf '\n--- %d passed, %d failed ---\n' "$_PASS" "$_FAIL"
  [ "$_FAIL" -eq 0 ]
}
