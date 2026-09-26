<!-- hoshivel:agent-rules v1 -> https://github.com/Hoshivel/workspace -->

# AGENTS.md — hoshi-ci

> 共通流程以 [workspace](https://github.com/Hoshivel/workspace) 的 `AGENTS.md`
> 為準；本檔只列本倉庫規則。

## 0. 開工前

1. 讀 `../workspace/focus.md` 與 `../workspace/AGENTS.md`；缺少時先
   `git clone https://github.com/Hoshivel/workspace.git ../workspace`，
   取不到就停止並說明。
2. 待辦與日誌在 `workspace/todo/hoshi-ci/`、`workspace/logs/hoshi-ci/`；
   不得在本倉庫另建副本。

## 1. 入場閱讀順序

1. `README.md` —— 做什麼、公開日誌裡有什麼、token 出現在哪裡。
2. `tools/run-job.py` 與 `tools/dispatch.py` 的檔頭。
3. `../hoshi-platform-standards/engineering/supply-chain.md` §1、§7、§8。

## 2. 驗證

改動後執行；綠燈再更新該事項的 `Status:`（`Editing` → `待驗證`）：

- `python tools/dispatch.py selftest`
- `python tools/run-job.py selftest`（需要 PyYAML）
- `python tools/check-workflows.py selftest` 與 `python tools/check-workflows.py`
- `python tools/run-job.py plan --src ../<倉庫>`：`targets.json` 裡每個倉庫各一次，
  都解析得了才算數。

## 3. 這個倉庫的特殊規則

- **本倉庫是公開的。** 拿得到 `HOSHIVEL_CI_TOKEN` 的 workflow 只能由 `schedule` 與
  `workflow_dispatch` 觸發，不得用快取或上傳 artifact；由 `tools/check-workflows.py` 擋下。
- token 只能交給列分支、checkout 與 `go mod download`；被驗證的程式碼開始執行之後的
  步驟不得再拿到它。
- 被驗證倉庫的輸出不得原樣印進日誌；新增可公開的輸出行要改 `run-job.py` 的 `SHOWN`，
  並在 selftest 補一行。
- 不在這裡重列被驗證倉庫的指令：`windows.yml` 照那個 commit 自己的 `verify-windows` 跑。
  `run-job.py` 認不得的形狀是錯誤，不得改成跳過。
- 依 workspace `AGENTS.md` §1.10.1，本倉庫只寫倉庫名與 commit；不寫私有倉庫的拓撲、
  服務關係或部署結構。
- 文件與註解沿用倉庫既有風格：**正體中文為主**（程式碼註解英文），
  狀態關鍵字保持原樣以利機器辨識。
