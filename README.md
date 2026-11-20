# hoshi-ci

自動代跑來源由 `targets.json` 的 `enabled_windows` 明示啟用，初始為空。
先完成具名試跑，再切換來源倉庫的自動 Windows 工作，避免兩邊同時執行。

在這個公開倉庫的 runner 上，代跑 Hoshivel 私有倉庫的 Windows 原生驗證。

## 做什麼

- **`派工`**（`dispatch.yml`）：每 15 分鐘與手動。列出啟用來源的同倉 Ready PR，
  驗證以 main 為基底的 GitHub 合併提交及其兩個父提交。每輪最多派兩項。
  Windows 分配前及私有 checkout 前各重新核對，草稿與過時提交不做來源工作。
- **`Windows`**（`windows.yml`）：checkout 那個 commit，讀它自己
  `.github/workflows/ci.yml` 的 `verify-windows` job，照步驟在 `windows-latest` 上跑。
  這裡不另寫指令——要改驗證內容，改那個倉庫的 job。
- run 的標題是 `Windows <倉庫>@<merge commit>`。合併者須核對 PR 的當前合併提交，
  並確認 admission 與 verify-windows 都成功，過時來源的 skipped 不算通過。
  一個合併提交已有 run（不論結果）就不會自動重派。歷史超過完整掃描上限時拒絕派工。
  失敗先診斷，明示重試由操作者處理。

被驗證的倉庫不持有任何指向這裡的憑證：hoshi-ci 主動去拉，沒有人往這裡推。

## 公開日誌裡有什麼

- 倉庫名、合併 commit、Step 編號、成敗與時間。來源步驟名稱、編譯錯誤及測試正文
  留在短生命週期 runner，不輸出到公開日誌。
- 不上傳 artifact，不留任何快取。
- 要看失敗的細節：在 Windows 開發機 checkout 同一個 commit，重跑失敗的那一步。

## 憑證

`HOSHIVEL_CI_TOKEN`（organization secret，Contents 與 Pull requests: Read-only）只用於核對來源、
取得被驗證的 commit，以及 `go mod download`。後兩處都以 `GIT_CONFIG_*` 行程環境變數帶入
insteadOf，不寫進設定檔；被驗證的程式碼在那之後才執行，拿不到它。取得 commit 不用
`actions/checkout`，因為它會把 commit 標題印進日誌。

拿得到它的兩支 workflow 只由排程與手動觸發。這些承諾由 `tools/check-workflows.py` 檢查，
`CI` 每次都跑。

## 手動

```sh
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci -f dry_run=true
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci -f repo=hoshi-identity
gh workflow run windows.yml -R Hoshivel/hoshi-ci -f repo=hoshi-data -f sha=<40 字元 commit>
gh run list -R Hoshivel/hoshi-ci -w windows.yml
```

手動試跑仍須使用當前 Ready PR 的合併提交。這裡不要求對私有倉庫的寫入權限，
結果由上述精確提交的公開 run 提供。其餘測試與安全掃描沿用原私有工作流。
