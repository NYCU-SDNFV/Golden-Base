# SDNFV Golden Base — 共用 Makefile 骨架
# 各 Lab 覆寫 build/up/down/lab-test，但 policy / test 兩個 target 不要改。
SHELL := /usr/bin/env bash

.PHONY: help build up down shell logs clean policy lab-test test

help:
	@echo "make build     建置環境"
	@echo "make up        啟動環境"
	@echo "make down      停止環境"
	@echo "make test      跑完整檢查（policy + lab-test）"
	@echo "make policy    只跑 Base 規範檢查"
	@echo "make clean     清乾淨"

build:      ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 build"
up:         ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 up"
down:       ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 down"
shell:      ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 shell"
logs:       ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 logs"
clean:      ## 各 Lab 覆寫
	@echo "(base) 此 Lab 未定義 clean"

# --- 以下由 Base 提供，請勿修改（.github/ 每次提交都會被還原） ---
policy:
	@bash .github/policy/00_layout.sh
	@bash .github/policy/01_integrity.sh

lab-test:
	@if [ -x .github/tests/run.sh ]; then bash .github/tests/run.sh; \
	 else echo "(base) 此 Lab 未定義 lab-test"; fi

test: policy lab-test
