# 📑 DB2 跨 Data Center 儲存搬遷自動化設計規格書

## 一、 專案背景與目標

為了將 DB2 Cluster 的 Standby Server 移轉至第二座 Data Center (DC2)，我們將透過 KVM Image 移轉技術在 DC2 啟動該 Server。啟動後，該 Server 必須將其儲存後端從 DC1 的 Storage 線上移轉至 DC2 的 Storage。

內部決議使用 Linux LVM 的 `pvmove` 指令達到線上不中斷服務（Online Live Migration）的搬遷。本專案旨在透過 **Netflix Conductor** 結合 **Python Worker** 實作全自動化、具備冪等性、跨天執行且安全的移轉腳本。

## 二、 核心架構設計 (Conductor Workflow)

本專案拒絕在 Worker 內部使用長時間的 `sleep` 以防 Task Timeout，而是利用 Conductor 的 `DO_WHILE` 循環搭配 `WAIT` 系統任務，讓 Worker 保持無狀態（Stateless）且快速結束。

### 🔄 完整工作流執行拓樸

```
【 Workflow 啟動 】
       │
       ├──► [Task 1: add_storage_paths] (一次性，Step 1 設置)
       ├──► [Task 2: connect_verify_paths] (一次性，Step 2 驗證)
       ├──► [Task 3: prepare_migration_plan] (一次性，Step 3-1 & 3-2 初始化/載入進度)
       │
       └──► 【 DO_WHILE 循環開始 】 (每天 08:30 ~ 16:00 巡邏)
                 │
                 ├──► [Task 4: check_and_move_next] (核心狀態機 Worker)
                 │          │
                 │          ├─── (條件滿足：進行中或開搬下一顆)
                 │          │      ▼
                 │          └──► [Task 5: wait_step] (WAIT 系統任務，暫停 10 分鐘) ──┐
                 │                                                                 │
                 └──◄──────────────────────────────────────────────────────────────┘
```

## 三、 詳細步驟與冪等性（Idempotency）設計

為了支援**跨天中斷、隔天自動續傳**的維運情境，所有任務節點均導入「狀態先行檢查」的防禦性邏輯：

### 📥 Step 1: Add New Storage Path (`add_storage_paths`)

- **輸入參數：** 來自 PM 分配的 `storage_ip_info` (8 個 DC2 儲存 IP) 與 `target_sn`。
- **執行邏輯：**
  1. **自動備份：** 複製 `/etc/nvme/discovery.conf` 為備份檔。
  2. **網段比對：** 讀取原檔案， Distinct 出既有的 4 組舊網段 IP，並與輸入的 8 個新 IP 進行第三碼網段 Mapping。
  3. **冪等檢查：** 檢查檔案結尾，**若該 Mapping 組合（新 IP）已存在，則直接跳過不寫入**；若不存在，則以原本格式 Append 寫入檔案。

### 🔎 Step 2: Connect and Check Path (`connect_verify_paths`)

- **執行邏輯：**
  1. 執行 `nvme connect-all`（核心原生支援冪等，重複執行無害）。
  2. 對 8 個新 IP 進行巡檢，執行 `nvme list-sub | grep '{IP}' | grep live | wc -l`。
  3. **安全閘門：** 8 個 IP 的結果必須**全數為 1**。只要有任一 IP 狀態不為 live，此 Task 宣告 `FAILED`，中止工作流。

### 📋 Step 3-1 & 3-2: Prepare Migration Plan (`prepare_migration_plan`)

- **狀態源（Source of Truth）：** 儲存於 Target Server 上的 `migration_plan.json`。
- **執行邏輯：**
  1. **防覆蓋檢查：** 檢查該 JSON 檔案是否存在。
  2. **若檔案存在（第二天續傳）：** 直接讀取昨天的檔案內容作為 Task Output，**絕對不重新初始化**。
  3. **若檔案不存在（第一天啟動）：** * 執行 `nvme list`，依據 `source_sn` 與 `target_sn` 解析出 Source (8顆) 與 Target (10顆) 的 Node 列表。
     - **容量與效能對齊（盲點優化）：** 嚴格依據容量大小（例如 107 GB 對齊 107 GB）進行 1 對 1 的 Mapping 配對，初始化產生 `migration_plan.json`，初始狀態皆為 `Non-Open`。

## 四、 核心巡檢狀態機邏輯 (`check_and_move_next`)

此 Worker 每次被 Conductor 喚醒（每 10 分鐘一次）進入後，會依據以下權重與順序進行**嚴格的三維狀態檢查**：

### ⏰ 關卡 1：時間維護視窗檢查（Time Window）

- 檢查當前系統時間是否大於 **`16:00`**？
- **是：** 代表今日維護視窗已過。**不啟動任何新任務**，讓背景正在跑的 `pvmove` 繼續跑完。Worker 直接回傳 `status: TIME_WINDOW_CLOSED` 結束本次週期（工作流進入安全空轉）。

### 🔒 關卡 2：當前 LVM 與 進程狀態檢查（金融航太級三合一校驗）

讀取 JSON 紀錄中狀態為 `On-Going` 的那筆 PV，下達指令交叉驗證：

1. `check_ps`：是否有 `pvmove` 的系統進程在跑？
2. `check_lvs`：LVM 底層是否殘留 `[pvmove0]` 臨時鏡像？
3. `check_pe`：執行 `pvdisplay -m`，該 Source PV 的 `Allocated PE` 是否為 0？

#### 根據校驗結果，Worker 走入以下三大分支之一：

| 分支情境            | `ps`進程 | `lvs` 鏡像     | `Allocated PE` | 判定結果與後續動作                                           |
| ------------------- | -------- | -------------- | -------------- | ------------------------------------------------------------ |
| **A. 跨夜搬移中**   | 存在     | 存在           | >0             | **正常搬移中**。回傳 `status: RUNNING`，交由 Conductor WAIT 10分鐘後再檢查。 |
| **B. 雙重驗證成功** | 不存在   | 不存在         | **等於 0**     | **搬遷真正成功！** 1. 將該 PV 在 JSON 標記為 `Done`。 2. 繼續往下前進至【關卡 3】開搬下一顆。 |
| **C. 攔截寂靜失敗** | 不存在   | 存在 或 不存在 | **大於 0**     | **⚠️ 異常中斷（如網路閃斷/人為中止）！** 資料未搬完但進程死了。Worker **直接拋出 Exception 讓 Task FAILED**，工作流立刻暫停（PAUSED），等待工程師人工排查。 |

### 🚀 關卡 3：派發下一組搬遷任務

若目前系統無任何 `On-Going` 任務，Worker 從 JSON 中挑選第一個 `Non-Open` 的配對組合：

1. **加入群組（與容量盲點對齊）：** 針對該 Target PV 執行 `pvcreate` 與 `vgextend <VG> <Target_PV>`，使其合法。*(內部具備 pvs 檢查，若已加入則自動跳過，保持冪等)*。
2. **背景開搬：** 執行 `nohup pvmove <Source_PV> <Target_PV> > pvmove_xxx.log 2>&1 &`。
3. **更新紀錄：** 將 JSON 裡該組狀態改為 `On-Going`。
4. 回傳 `status: NEXT_STARTED`。

## 五、 大結局收尾階段（Final Clean up）

當 Worker 檢查 JSON 發現 **8 個 PV 的狀態全數變為 `Done`** 時，代表所有 DB2 資料皆已安全轉移至 DC2。此時，Worker 將一口氣執行最終收尾，絕不在中途「拆橋」，以保留最大容災回滾（Rollback）彈性。

1. **批次剔除舊硬碟：** 一個指令將 8 顆已空置的 DC1 硬碟移出 VG。

   Bash

   ```
   vgreduce mm_datavg /dev/nvme1n1 /dev/nvme1n2 ... /dev/nvme1n8
   ```

2. **抹除 LVM 殘留標籤：** 執行 `pvremove /dev/nvme1n1 ...`，將舊硬碟還原為乾淨原始狀態。

3. **環境斷捨離（斷開舊路徑）：**

   - 修改 `/etc/nvme/discovery.conf`，徹底清除或註解掉第一天加進去的 8 行 DC1 儲存路徑。
   - 執行 `nvme disconnect -n <DC1_Storage_NQN>`，乾淨利落地中斷與 DC1 的所有網路連線，防止核心噴出 I/O Timeout 錯誤。

4. 回傳 `status: ALL_COMPLETED`，`is_all_done: true`，Conductor 偵測到迴圈條件不滿足，**整個大工作流圓滿結束**。