# SDNFV Golden Base

## Shared Golden publication

Golden-Base also owns the versioned common publisher and reusable release
workflow. See [the release guide](tools/release/README.md).

Each private `Golden-<Name>` keeps its exercises, rubric, Lab-specific checks,
environment profile and release configuration. Golden-Base supplies the
canonical grading runtime, host bootstrap, pretest and publisher. A thin caller
runs the Lab-specific PoC, then invokes the publisher at a pinned Golden-Base
commit. Do not maintain separate copies of these implementations per Lab.

## Shared runtime and Lab profiles

| Responsibility | Owner |
|---|---|
| Image resource thresholds and container limits | Pinned `lab-base` image, `lab_resources` |
| Disposable hosted-runner preparation and verification | Golden runtime |
| Non-scoring `make pretest` and repair instructions | Golden runtime |
| Release/integrity gates, timeouts, result format | Golden runtime |
| Exercise code, topology, rubric, report and checkpoint tests | Individual Lab |
| Datapath and kernel feature selection | Protected per-Lab profile |
| Release channel, assignment and manual updates | Per-Lab configuration |

Current environment profiles are `toolchain` (userspace OVS), `controller`
(userspace OVS and OpenFlow), `measurement` (kernel OVS, BBR and qdiscs), and
`vrouter` (kernel OVS, FRR, dual-stack routing and VXLAN).

Automatic host changes are restricted to the disposable native Linux
GitHub-hosted course runner. Local and self-hosted machines require an explicit
administrator action. `make pretest` never completes an exercise or changes
host configuration; it runs isolated diagnostics and explains failures.
See [the runtime guide](tools/runtime/golden/README.md).

The source profile is `.github/golden/profile.json`. Runtime code is generated
from this repository, protected by the student manifest, and checked against
the exact publisher pin. The publisher injects its own verified implementation
into the canonical bundle. A template copy is **not** live inheritance:
updating Golden-Base alone does not update existing Labs or accepted work.

The publication credential is an organization secret restricted to **selected
instructor repositories**, never `all` or `private`: student repositories are
private too. The shared workflow supports a scoped-token backend and a GitHub
App backend. Active token deployments rotate one central credential, not one
credential per Lab. The GitHub App backend can replace it without changing Lab
code.

Use [golden-bootstrap.py](tools/golden-bootstrap.py) from a local administrator
session (`GOLDEN_ADMIN_TOKEN`, never committed or placed in a student workflow):

```sh
python3 tools/golden-bootstrap.py --name VRouter \
  --source /path/to/Golden-VRouter --toolkit-ref FULL_TESTED_GOLDEN_BASE_SHA \
  --poc-workflow poc-lab3.yml --replace-workflow
```

The default is a read-only plan. Add `--apply` after reviewing it. Bootstrap
creates the private repository if needed, verifies the instructor-only access
boundary, grants the instructors team, enrolls the repository ID in the existing
org secret and generates the pinned caller. It does not commit or push Lab
code. New Labs can supply `--slug`, `--title`, `--description` and `--semester`
to generate their release configuration, plus `--lab-number`, `--checks` and
`--environment` to generate their runtime profile.

After configuring the Lab's Dockerfile, rubric and `make pretest` target,
materialize the shared runtime from a clean checkout of the **same tested pin**
used by the release caller:

```sh
python3 -B /path/to/Golden-Base/tools/runtime/sync.py \
  --source /path/to/Golden-VRouter --write
git -C /path/to/Golden-VRouter add .github/golden .github/grade/autograder.py \
  .github/policy/manifest.sha256
python3 -B /path/to/Golden-Base/tools/runtime/sync.py \
  --source /path/to/Golden-VRouter
```

This updates generated files and their protected hashes; it does not commit,
push, publish a release, or modify any student's answers. The Lab's PoC and
student grading must both use the canonical bundle, without a separate
workflow step that silently supplies missing host preparation.

After normal code review/commit, publishing remains an immutable tag push:

```sh
git tag newbie-v0.1.0
git push origin newbie-v0.1.0
```

Automatic student update PRs are a separate explicit policy. A credential
without Pull requests permission uses `student_updates: manual`: templates and
canonical grading still update, while students run the existing `make update`.
Do not silently swallow a failed PR request or grant broader access to student
repositories merely to make publication green.

## Base template skeleton

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
6. 宣告 runtime profile，使用上面的 pinned bootstrap / sync 流程
7. 在 Docker engine 的 Linux VM 執行 `make pretest`，再驗 `make test` 與 canonical PoC
8. 先發布 newbie，驗證真實學生更新與評分，再另行核准正式 channel

## ⛔ 絕對不要放進 template

- `.github/workflows/autograde.yaml` — Classroom 50 在 accept 時會注入自己的 shim，
  template 裡有同名檔會在 submit resync 時覆蓋它，造成重複評分或評分完全失效。
- 任何解答：`*.solution.*`、`LAB*-SOLUTION.md`、標準答案 branch。
  **學生對 template repo 有 read 權限，看得到所有 branch 與完整 history。**
