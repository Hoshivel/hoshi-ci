# hoshi-ci

在這個公開倉庫的 runner 上，代跑 Hoshivel 私有倉庫的 Windows 原生驗證。

## 做什麼

- **`派工`**（`dispatch.yml`）：每 15 分鐘與手動。列出 [`targets.json`](targets.json) 裡
  每個倉庫的分支 head，還沒有 run 的 commit 各派一次 `Windows`。排程會延遲，等結果時
  不必等它，手動派一次即可（見下方〈手動〉）。
- **`Windows`**（`windows.yml`）：checkout 那個 commit，讀它自己
  `.github/workflows/ci.yml` 的 `verify-windows` job，照步驟在 `windows-latest` 上跑。
  這裡不另寫指令——要改驗證內容，改那個倉庫的 job。
- run 的標題是 `Windows <倉庫>@<commit>`，那就是結果的記錄；別處沒有狀態。
  一個 commit 已經有 run（不論結果）就不會再被派，要重跑用 `gh run rerun`。

被驗證的倉庫不持有任何指向這裡的憑證：hoshi-ci 主動去拉，沒有人往這裡推。

## 公開日誌裡有什麼

- 倉庫名、commit，以及每一步的名稱、成敗與時間。失敗的那一步只印失敗的測試、套件與
  工具鏈錯誤，不印原始碼、commit 訊息、斷言訊息或測試輸出。
- 不上傳 artifact，不留任何快取。
- 要看失敗的細節：在 Windows 開發機 checkout 同一個 commit，重跑失敗的那一步。

## 憑證

`HOSHIVEL_CI_TOKEN`（organization secret，Contents: Read-only）只出現在三處：派工時列分支、
取得被驗證的 commit，以及 `go mod download`。後兩處都以 `GIT_CONFIG_*` 行程環境變數帶入
insteadOf，不寫進設定檔；被驗證的程式碼在那之後才執行，拿不到它。取得 commit 不用
`actions/checkout`，因為它會把 commit 標題印進日誌。

拿得到它的兩支 workflow 只由排程與手動觸發。這些承諾由 `tools/check-workflows.py` 檢查，
`CI` 每次都跑。

## 手動

```sh
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci
gh workflow run windows.yml -R Hoshivel/hoshi-ci -f repo=hoshi-data -f sha=<40 字元 commit>
gh run list -R Hoshivel/hoshi-ci -w windows.yml
```
