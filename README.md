# hoshi-ci

自動代跑來源由 `targets.json` 的 `enabled_windows` 明示啟用，目前只有 `hoshi-data`。
其餘列在 `windows` 的來源維持受控手動派工。先完成具名試跑，再切換來源倉庫的
自動 Windows 工作，避免兩邊同時執行。

在這個公開倉庫的 runner 上，代跑 Hoshivel 私有倉庫的 Windows 原生驗證。

## 做什麼

- **`派工`**（`dispatch.yml`）：每 15 分鐘與手動。列出啟用來源的同倉 Ready PR，
  為每個新 head 凍結 GitHub 合併提交。第一個父提交記為凍結 base，第二個須等於 Ready head。
  派工器從 main ref 讀取主線，驗證 PR 回傳的 base、凍結 base 與 main 依序相等或具有祖先關係，
  容許 PR 的 base 欄位晚於合併提交更新。每輪最多派兩項。Windows 分配前及私有 checkout 前
  各重新核對，草稿與過時提交不做來源工作。
- **`Windows`**（`windows.yml`）：checkout 已凍結的合併 commit，讀它自己
  `.github/workflows/ci.yml` 的 `verify-windows` job，照步驟在 `windows-latest` 上跑。
  這裡不另寫指令——要改驗證內容，改那個倉庫的 job。
- run 的標題是 `Windows <倉庫>@<PR head>`；工作輸入另記錄 PR 編號、base 與 merge SHA。
  合併前須確認來源 PR 的當前 head 與 run 標題相同，並讀回該次工作輸入的 PR 編號、
  head、凍結的 base 與 merge；實際 checkout 必須是該 merge。
  `Ready PR admission` 與 `verify-windows` 兩個 job 均須實際成功，其中執行來源步驟的
  `verify-windows` step 也須成功，並保留來源倉庫其餘原有檢查的成功要求。
  派工成功、歷史 head 的綠燈或來源 Windows 的 skipped 都不能替代這些結果。
  main 正常前進時，原工作仍對應同一份凍結合併樹。主線已不含凍結 base 時拒絕執行。
  工作記錄凍結 base，因此能檢查它是否仍在 main 歷史；若主線改動保留該 base，
  無法從工作輸入判定其間是否曾改寫。
  一個 head 已有 run（不論結果，包括尚未執行驗證的 admission 失敗）就不會自動重派。
  歷史超過完整掃描上限時拒絕派工。失敗先診斷，明示重試由操作者處理。

被驗證的倉庫不持有任何指向這裡的憑證：hoshi-ci 主動去拉，沒有人往這裡推。

## 公開日誌裡有什麼

- 倉庫名、PR 編號、head/base/merge SHA、Step 編號、成敗與時間。來源步驟名稱、編譯錯誤及測試正文
  留在短生命週期 runner，不輸出到公開日誌。
- 不上傳 artifact，不留任何快取。
- 要看失敗的細節：在 Windows 開發機 checkout 同一個 commit，重跑失敗的那一步。

## 憑證

`HOSHIVEL_CI_TOKEN`（organization secret，Contents 與 Pull requests: Read-only）只用於核對來源、
取得被驗證的 commit，以及 `go mod download`。後兩處都以 `GIT_CONFIG_*` 行程環境變數帶入
insteadOf，不寫進設定檔；被驗證的程式碼在那之後才執行，拿不到它。取得 commit 不用
`actions/checkout`，因為它會把 commit 標題印進日誌。
若 PR 讀取回傳 HTTP 403，須檢查此 token 的 **Pull requests: read** 與來源倉庫存取範圍；
只有 Contents: read 能 clone，不足以通過 Ready PR 準入。

拿得到它的兩支 workflow 只由排程與手動觸發。這些承諾由 `tools/check-workflows.py` 檢查，
`CI` 每次都跑。

## 排程未觸發時

`active` 或 `workflow_dispatch` 成功不能代替自然排程驗收；須實際讀到 `event=schedule`
的 run 並核對派工結果。[GitHub 官方排查](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflow-stopped-running)
說明已啟用的排程仍可能停止，有寫入權限的使用者可修改 cron 重新指定 actor；這個恢復動作
不等於已證明原 actor 失效，也不保證立即觸發。排程亦可能因平台負載延遲或被捨棄。

等待期間保留具名手動入口。先查相同 head 的 run 與尚未辨識來源的 queued 工作，
確認沒有重複後才補派；缺少中央真實成功結果時仍不得以來源 skipped 放行合併。

## 手動

```sh
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci -f dry_run=true
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci -f repo=hoshi-identity
gh workflow run dispatch.yml -R Hoshivel/hoshi-ci -f repo=hoshi-data -f pr=<PR 編號> -f dry_run=true
gh workflow run windows.yml -R Hoshivel/hoshi-ci -f repo=hoshi-data -f pr=<PR 編號> \
  -f sha=<40 字元 PR head> -f base=<40 字元凍結 base> -f merge=<40 字元凍結合併提交>
gh run list -R Hoshivel/hoshi-ci -w windows.yml
```

手動試跑也必須提供全部五項來源身分，通過同倉、open、Ready、以 main 為目標、head 未變
與凍結 merge 的兩個父提交完全匹配等核對，不能任意指定一個 commit 執行。
重試先保留原工作輸入；若 PR head 已變，改由新 head 派工。這裡不要求對私有倉庫的寫入權限，
結果由上述精確提交的公開 run 提供。其餘測試與安全掃描沿用原私有工作流。
