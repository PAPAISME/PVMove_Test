import io
import json
import os
import subprocess
from datetime import datetime

# 試圖載入 textfsm，若未安裝則噴出友善提示
try:
    import textfsm
except ImportError:
    raise ImportError("請先執行 'pip install textfsm' 以支援 TextFSM 模板解析功能。")

# ==========================================
# 全域常數設定
# ==========================================
PLAN_FILE_PATH = "/opt/migration/migration_plan.json"
NVME_CONF_PATH = "/etc/nvme/discovery.conf"
TIME_WINDOW_LIMIT = "16:00"

# ==========================================
# 輔助工具函式 (Helper Functions)
# ==========================================

def run_command(cmd):
    """執行 Linux Shell 指令並回傳 stdout，失敗時拋出 Exception"""
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"指令執行失敗: {cmd}\nError: {result.stderr.strip()}")
    return result.stdout.strip()

def load_plan():
    if os.path.exists(PLAN_FILE_PATH):
        with open(PLAN_FILE_PATH, 'r') as f:
            return json.load(f)
    return None

def save_plan(plan):
    os.makedirs(os.path.dirname(PLAN_FILE_PATH), exist_ok=True)
    with open(PLAN_FILE_PATH, 'w') as f:
        json.dump(plan, f, indent=4)

# ==========================================
# Task Handler 1: [TextFSM 完全參數化版] Step 1 增加儲存路徑
# ==========================================
def add_storage_paths(task):
    """
    Step 1: 利用 TextFSM 模板完全參數化解析 discovery.conf，
    動態繼承原始環境的 TRSVCID 與 TOS，並依網段對齊 Append 新路徑。
    
    Input 格式範例:
    task.input_data = {
        "storage_ip_info": [
            "100.98.68.47", "100.98.68.48",
            "100.98.69.47", "100.98.69.48",
            "100.98.70.47", "100.98.70.48",
            "100.98.71.47", "100.98.71.48"
        ]
    }
    """
    input_ips = task.input_data.get("storage_ip_info", [])
    
    # 1. 備份原有設定檔
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    run_command(f"cp {NVME_CONF_PATH} {NVME_CONF_PATH}.bak_{timestamp}")
    
    # 2. 定義完全參數化的 TextFSM 模板
    template_content = """Value LOCAL_IP (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})
Value STORAGE_IP (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})
Value TRSVCID (\d+)
Value TOS (\d+)

Start
  ^-t rdma -w ${LOCAL_IP} -a ${STORAGE_IP} --trsvcid ${TRSVCID} --tos ${TOS} -> Record
"""
    
    # 3. 讀取設定檔並丟入 TextFSM 解析
    with open(NVME_CONF_PATH, 'r') as f:
        config_data = f.read()
        
    re_table = textfsm.TextFSM(io.StringIO(template_content))
    parsed_results = re_table.ParseTextToDicts(config_data)
    
    # 4. 建立網段對應表，同時保存該網段對應的 TRSVCID 與 TOS
    # 結構範例: { "68": {"local_ip": "100.98.68.92", "trsvcid": "4420", "tos": "106"}, ... }
    local_segment_map = {}
    for row in parsed_results:
        local_ip = row["LOCAL_IP"]
        segment = local_ip.split('.')[2]  # 取得第三碼網段 (e.g., "68")
        
        # 透過 Dictionary 特性達到網段的 Distinct，若有重複網段則保留第一筆或自動覆蓋
        if segment not in local_segment_map:
            local_segment_map[segment] = {
                "local_ip": local_ip,
                "trsvcid": row["TRSVCID"],
                "tos": row["TOS"]
            }

    if not local_segment_map:
        raise RuntimeError("TextFSM 無法從 discovery.conf 中解析出任何有效的原始儲存設定行！")

    # 5. 根據網段對齊，動態帶入 TRSVCID 與 TOS 組合新配置
    lines_to_append = []
    for new_storage_ip in input_ips:
        new_segment = new_storage_ip.split('.')[2]
        cfg_meta = local_segment_map.get(new_segment)
        
        if not cfg_meta:
            raise RuntimeError(f"環境異常：新儲存端 IP {new_storage_ip} 的網段 '{new_segment}' 無法對應到任何原始本地環境設定！")
            
        # 完美動態繼承環境中的 trsvcid 與 tos 參數
        new_line = f"-t rdma -w {cfg_meta['local_ip']} -a {new_storage_ip} --trsvcid {cfg_meta['trsvcid']} --tos {cfg_meta['tos']}"
        
        # 冪等性檢查：確認這行沒被加過，才放進準備清單
        if new_line not in config_data:
            lines_to_append.append(new_line)
            
    # 6. 將新路徑 Append 至檔案末尾
    if lines_to_append:
        with open(NVME_CONF_PATH, 'a') as f:
            for line in lines_to_append:
                f.write(f"\n{line}  ## new storage")
                
    return {
        "status": "COMPLETED", 
        "output": {
            "message": f"TextFSM 成功解析原始設定。動態繼承參數並新寫入 {len(lines_to_append)} 行配置。",
            "active_segments": list(local_segment_map.keys())
        }
    }

# ==========================================
# Task Handler 2: Step 2 連線與路徑檢查
# ==========================================
def connect_and_verify_paths(task):
    ip_info = task.input_data.get("storage_ip_info", [])
    
    # 1. 執行連線
    run_command("nvme connect-all")
    
    # 2. 巡檢 8 個新 IP 狀態是否皆為 live
    for ip in ip_info:
        cmd = f"nvme list-sub | grep '{ip}' | grep live | wc -l"
        count = run_command(cmd)
        if count.strip() != "1":
            # 觸發安全閘門：直接讓 Task 失敗，中斷工作流
            raise RuntimeError(f"安全警報：IP {ip} 的 NVMe 路徑未處於 live 狀態！")
            
    return {"status": "COMPLETED", "output": {"message": "All paths verified live"}}

# ==========================================
# Task Handler 3: Step 3-1 & 3-2 初始化/載入計畫
# ==========================================
def prepare_migration_plan(task):
    source_sn = task.input_data.get("storage_sn_info", {}).get("source_sn")
    target_sn = task.input_data.get("storage_sn_info", {}).get("target_sn")
    
    plan = load_plan()
    if plan is not None:
        # 第二天續傳：直接使用既有計畫
        return {"status": "COMPLETED", "output": {"plan": plan}}
        
    # 第一天啟動：解析 nvme list 並建立容量對齊的 Mapping
    # 以下為模擬解析完後的容量嚴格對齊結果範例（實際佈署時請依據 nvme list 輸出的真實 SN 進行 map 串接）：
    mock_pairs = [
        {"source_pv": "/dev/nvme1n1", "target_pv": "/dev/nvme3n1", "vg_name": "mm_datavg", "status": "Non-Open"},
        {"source_pv": "/dev/nvme1n2", "target_pv": "/dev/nvme3n2", "vg_name": "mm_datavg", "status": "Non-Open"},
        # 可依此類推串好 8 個配對...
    ]
    
    new_plan = {
        "migration_date": datetime.now().strftime("%Y-%m-%d"),
        "pairs": mock_pairs
    }
    save_plan(new_plan)
    
    return {"status": "COMPLETED", "output": {"plan": new_plan}}

# ==========================================
# Task Handler 4: 核心循環狀態機 (Step 3-3, 3-4, 4 & 收尾)
# ==========================================
def check_and_move_next(task):
    """
    優化版核心循環狀態機：
    1. 優先判定全數完工與最後一顆交界條件
    2. 支援 canary_limit 參數，可於生產環境安全測試前 N 顆
    """
    # 接收 Conductor 傳入的測試參數，預設 0 代表不限制 (全跑)
    canary_limit = int(task.input_data.get("canary_limit", 0))
    
    plan = load_plan()
    if not plan:
        raise RuntimeError("找不到遷移計畫檔案！")
        
    pairs = plan["pairs"]
    total_pairs_count = len(pairs)
    
    # 動態計算：如果啟動了金絲雀測試，則「目標完工數」就是 canary_limit，否則就是總數
    target_done_count = canary_limit if canary_limit > 0 else total_pairs_count

    # --------------------------------------------------------
    # 關卡 1：金安級三合一狀態校驗 (處理正在進行中的任務)
    # --------------------------------------------------------
    ps_count = int(run_command("ps -ef | grep 'pvmove' | grep -v grep | wc -l"))
    lvs_count = int(run_command("lvs -a | grep '\[pvmove0\]' | wc -l"))
    
    ongoing_pair = next((p for p in pairs if p["status"] == "On-Going"), None)
    
    if ongoing_pair:
        source_pv = ongoing_pair["source_pv"]
        allocated_pe = int(run_command(f"pvs --noheadings -o pv_alloc {source_pv}").strip())
        
        if ps_count > 0 and lvs_count > 0:
            # 分支 A：正常搬移中
            return {"status": "COMPLETED", "output": {"loop_status": "RUNNING", "is_all_done": False}}
            
        elif ps_count == 0 and lvs_count == 0 and allocated_pe == 0:
            # 分支 B：此顆完美完工！
            ongoing_pair["status"] = "Done"
            save_plan(plan)
        else:
            # 分支 C：攔截寂靜失敗，直接阻斷
            raise RuntimeError(f"⚠️ 安全警報：{source_pv} 搬移進程異常消失，但仍殘留 {allocated_pe} 個 PE 未搬完！")

    # --------------------------------------------------------
    # 關卡 2：完工條件與金絲雀測試（Canary Check）判定
    # --------------------------------------------------------
    done_count = sum(1 for p in pairs if p["status"] == "Done")
    
    # 只要達成了目標完工數（不管是 8 顆全部搬完，還是測試用的前 2 顆搬完）
    if done_count >= target_done_count:
        # 執行大結局收尾階段 (vgreduce, pvremove)
        all_sources_done = " ".join([p["source_pv"] for p in pairs if p["status"] == "Done"])
        vg_name = pairs[0]["vg_name"]
        
        # 批次剔除與抹除已完成的 PV
        run_command(f"vgreduce {vg_name} {all_sources_done}")
        run_command(f"pvremove {all_sources_done}")
        
        # 提示目前的完工狀態
        msg = f"金絲雀測試成功中止，已完成前 {done_count} 顆！" if canary_limit > 0 else "8 顆硬碟全數遷移成功！"
        
        return {
            "status": "COMPLETED", 
            "output": {"loop_status": "ALL_COMPLETED", "is_all_done": True, "message": msg}
        }

    # --------------------------------------------------------
    # 關卡 3：時間維護視窗檢查 (只有在「還要繼續開新任務」時才檢查)
    # --------------------------------------------------------
    current_time = datetime.now().strftime("%H:%M")
    if current_time > TIME_WINDOW_LIMIT:
        return {
            "status": "COMPLETED", 
            "output": {"loop_status": "TIME_WINDOW_CLOSED", "is_all_done": False}
        }

    # --------------------------------------------------------
    # 關卡 4：派發下一個 Non-Open 任務
    # --------------------------------------------------------
    next_pair = next((p for p in pairs if p["status"] == "Non-Open"), None)
    
    if next_pair:
        tgt_pv = next_pair["target_pv"]
        vg = next_pair["vg_name"]
        src_pv = next_pair["source_pv"]
        
        # 冪等性加入 VG
        vg_check = run_command(f"pvs {tgt_pv} --noheadings -o vg_name").strip()
        if vg_check != vg:
            run_command(f"pvcreate -y {tgt_pv}")
            run_command(f"vgextend {vg} {tgt_pv}")
            
        # 背景啟動 pvmove
        run_command(f"nohup pvmove {src_pv} {tgt_pv} > /opt/migration/pvmove_{os.path.basename(src_pv)}.log 2>&1 &")
        
        next_pair["status"] = "On-Going"
        save_plan(plan)
        
        return {"status": "COMPLETED", "output": {"loop_status": "NEXT_STARTED", "is_all_done": False}}
        
    return {"status": "COMPLETED", "output": {"loop_status": "UNKNOWN", "is_all_done": False}}