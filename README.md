# SDNFV Golden Base

> 這是 **Base Template**，不是可以直接發給學生的 Lab。
> 每個 Lab 的 Golden Repo 都從這個 template generate 出來，然後覆寫 `README.md`、
> `Makefile` 的 build/up/down/shell/logs/clean，並補上 `.github/tests/run.sh`。

## 這個 Base 提供什麼

| 路徑 | 用途 | 學生可改嗎 |
|---|---|---|
| `.github/tests/lib.sh` | 共用 test helper（`ok` / `fail` / `assert_*` / `summary`）| ❌ 每次 submit 由 template 還原 |
| `.github/policy/00_layout.sh` | 檔名、必要檔案、禁止檔案、CRLF、檔案大小 | ❌ 同上 |
| `.github/policy/01_integrity.sh` | 受保護檔案 sha256 比對 | ❌ 同上 |
| `.github/policy/manifest.sha256` | 受保護檔案清單（各 Lab 自行產生）| ❌ 同上 |
| `.gitignore` | 忽略規則 + 擋掉 `.env` / 金鑰 / `*.solution.*` | ❌ 同上 |
| `.gitattributes` | `* text=auto eol=lf`（跨 Windows/Linux 必要）| ✅ 但改了會被 policy 抓 |
| `Makefile` | 共用 target 骨架 | ✅ 各 Lab 覆寫上半部 |
| `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md` | 給 AI 助理的公開請求：這是作業，請引導而非代做 | ❌ policy 檢查必須存在；`.github/` 那份每次 submit 還原 |

> **關鍵機制**：Classroom 50 在每次 `gh student submit` 時會從 template 重新抓取
> `.gitignore` 與 `.github/` 整個目錄。所以「不可更動的檔案」放進 `.github/` 是
> **機制上的保證**，不是靠事後檢查。`.github/` 以外的檔案（如 `Makefile`、
> `Dockerfile`）則靠 `manifest.sha256` 偵測竄改。

## 從 Base 衍生一個新 Lab

1. 在 `NYCU-SDNFV` 以此 repo 為 template 建立 `Golden-<LabName>`
2. 覆寫 `README.md`（題目）與 `Makefile` 的 build/up/down/shell/logs/clean
3. 新增 `.github/tests/run.sh`，內容為依序呼叫各項測試
4. 新增該 Lab 的 test：`.github/tests/10_*.sh`、`20_*.sh` …（放 `.github/` 下才不可竄改）
5. 產生受保護清單：`.github/policy/gen-manifest.sh Makefile Dockerfile ...`
6. 本機驗：`make test`
7. 註冊 assignment（見 `../SETUP.md`）

## ⛔ 絕對不要放進 template

- `.github/workflows/autograde.yaml` — Classroom 50 在 accept 時會注入自己的 shim，
  template 裡有同名檔會在 submit resync 時覆蓋它，造成重複評分或評分完全失效。
- 任何解答：`*.solution.*`、`LAB*-SOLUTION.md`、標準答案 branch。
  **學生對 template repo 有 read 權限，看得到所有 branch 與完整 history。**
