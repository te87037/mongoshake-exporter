import time
import requests
import json
import sys
import os
import logging
from prometheus_client import start_http_server, Gauge, Counter

# --- 1. 設定日誌 (Logging Configuration) ---
# 設定格式：時間 - 等級 - 訊息
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- 2. 配置區塊 ---
TARGETS_ENV = os.environ.get('MONGO_SHAKE_TARGETS', 'default=127.0.0.1:9300')
EXPORTER_PORT = int(os.environ.get('EXPORTER_PORT', 9900))
CATEGORIES_ENV = os.environ.get('MONITOR_CATEGORIES', 'all').lower()

# 解析 TARGETS
def parse_targets(targets_str):
    targets = {}
    if not targets_str:
        return targets
    for item in targets_str.split(','):
        if '=' in item:
            name, hp = item.split('=', 1)
            targets[name.strip()] = hp.strip()
    return targets

TARGETS = parse_targets(TARGETS_ENV)

# 解析 CATEGORIES
ENABLED_CATEGORIES = set(CATEGORIES_ENV.split(','))
if 'all' in ENABLED_CATEGORIES:
    ENABLED_CATEGORIES = {'latency', 'throughput', 'status', 'queue'}

logger.info(f"Configuration Loaded:")
logger.info(f"  - Exporter Port: {EXPORTER_PORT}")
logger.info(f"  - Targets: {TARGETS}")
logger.info(f"  - Enabled Categories: {ENABLED_CATEGORIES}")

# --- 3. 定義 Prometheus Metrics ---
DELAY_GAUGE = Gauge('mongoshake_sync_delay_seconds', 'End-to-end replication delay in seconds', ['instance'])
FETCH_DELAY_GAUGE = Gauge('mongoshake_fetch_delay_seconds', 'Oplog fetch delay in seconds', ['instance'])
TPS_GAUGE = Gauge('mongoshake_tps_oplog', 'Transactions Per Second (Oplogs/sec)', ['instance'])
LOGS_GET_COUNTER = Gauge('mongoshake_logs_get_total', 'Total oplogs fetched from source', ['instance'])
LOGS_SUCCESS_COUNTER = Gauge('mongoshake_logs_success_total', 'Total oplogs successfully replicated to target', ['instance'])
WORKER_COUNT_COUNTER = Gauge('mongoshake_worker_count_total', 'Total oplogs processed by worker', ['instance'])
QUEUE_USED_GAUGE = Gauge('mongoshake_queue_used_ratio', 'Queue used ratio', ['instance', 'queue_type'])
BUFFER_USED_GAUGE = Gauge('mongoshake_buffer_used_ratio', 'Buffer used ratio', ['instance'])
PAUSE_STATUS_GAUGE = Gauge('mongoshake_pause_status', '1 if replication is paused, 0 otherwise', ['instance'])


def get_json_data(host, port, path):
    """從 MongoShake API 獲取 JSON 數據"""
    url = f"http://{host}:{port}{path}"
    try:
        response = requests.get(url, timeout=2) # 設定 2 秒超時，避免卡住日誌
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        # 使用 warning 而不是 error，避免因為瞬斷導致日誌爆炸，但仍能看到問題
        logger.warning(f"Failed to fetch {url}: {e}")
        return None

def collect_metrics_for_instance(instance_name, host, port):
    """收集單個實例的指標"""
    now_unix = int(time.time())
    
    # --- /repl ---
    if 'latency' in ENABLED_CATEGORIES or 'throughput' in ENABLED_CATEGORIES:
        repl_data = get_json_data(host, port, "/repl")
        if repl_data:
            try:
                if 'latency' in ENABLED_CATEGORIES:
                    lsn_unix = repl_data['lsn']['unix']
                    lsn_ack_unix = repl_data['lsn_ack']['unix']
                    
                    # [Modified] 抓取延遲：保留原邏輯，這裡反映來源端多久沒資料更新
                    FETCH_DELAY_GAUGE.labels(instance=instance_name).set(now_unix - lsn_unix)
                    
                    # [Modified] 同步延遲：加入判斷邏輯
                    # 如果 lsn (抓到的) 與 lsn_ack (寫入的) 差距極小(或相等)，代表完全同步
                    # 此時將延遲設為 0，避免因為 Source 沒資料導致 now_unix 持續增加而產生假延遲
                    if (lsn_unix - lsn_ack_unix) <= 0:
                         DELAY_GAUGE.labels(instance=instance_name).set(0)
                    else:
                         # 真的有落後，才計算時間差
                         DELAY_GAUGE.labels(instance=instance_name).set(now_unix - lsn_ack_unix)
                
                if 'throughput' in ENABLED_CATEGORIES:
                    LOGS_GET_COUNTER.labels(instance=instance_name).set(repl_data.get('logs_get', 0))
                    LOGS_SUCCESS_COUNTER.labels(instance=instance_name).set(repl_data.get('logs_success', 0))
                    TPS_GAUGE.labels(instance=instance_name).set(repl_data.get('tps', 0))
            except Exception as e:
                logger.error(f"[{instance_name}] Error parsing /repl: {e}")

    # --- /sentinel ---
    if 'status' in ENABLED_CATEGORIES:
        sentinel_data = get_json_data(host, port, "/sentinel")
        if sentinel_data:
            PAUSE_STATUS_GAUGE.labels(instance=instance_name).set(1 if sentinel_data.get('Pause') else 0)

    # --- /worker ---
    if 'throughput' in ENABLED_CATEGORIES:
        worker_data = get_json_data(host, port, "/worker")
        if worker_data:
            try:
                WORKER_COUNT_COUNTER.labels(instance=instance_name).set(worker_data.get('count', 0))
            except Exception as e:
                logger.error(f"[{instance_name}] Error parsing /worker: {e}")

    # --- /queue & /persist ---
    if 'queue' in ENABLED_CATEGORIES:
        queue_data = get_json_data(host, port, "/queue")
        if queue_data:
            try:
                q_size = queue_data.get('logs_queue_size', 128)
                num_queues = len(queue_data.get('syncer_inner_queue', []))
                logs_used = sum(q['logs_queue_used'] for q in queue_data['syncer_inner_queue'])
                pending_used = sum(q['pending_queue_used'] for q in queue_data['syncer_inner_queue'])
                total_logs_size = q_size * num_queues
                
                if total_logs_size > 0:
                    QUEUE_USED_GAUGE.labels(instance=instance_name, queue_type='logs').set(logs_used / total_logs_size)
                    QUEUE_USED_GAUGE.labels(instance=instance_name, queue_type='pending').set(pending_used / total_logs_size)
            except Exception as e:
                logger.error(f"[{instance_name}] Error parsing /queue: {e}")

        persist_data = get_json_data(host, port, "/persist")
        if persist_data:
            try:
                buffer_used = persist_data.get('buffer_used', 0)
                buffer_size = persist_data.get('buffer_size', 0)
                if buffer_size > 0:
                    BUFFER_USED_GAUGE.labels(instance=instance_name).set(buffer_used / buffer_size)
            except Exception as e:
                logger.error(f"[{instance_name}] Error parsing /persist: {e}")


def main_loop():
    if not TARGETS:
        logger.critical("No targets configured! Please check MONGO_SHAKE_TARGETS environment variable.")
        sys.exit(1)
        
    logger.info("Starting collection loop...")
    
    while True:
        start_time = time.time()
        success_count = 0
        
        for name, host_port in TARGETS.items():
            try:
                host, port = host_port.split(':')
                collect_metrics_for_instance(name, host, int(port))
                success_count += 1
            except ValueError:
                logger.error(f"Invalid target format: {name}={host_port}")
            except Exception as e:
                logger.error(f"Unexpected error collecting from {name}: {e}")
        
        duration = time.time() - start_time
        # 每次循環印出一條 INFO 日誌，證明活著
        logger.info(f"Collection cycle completed in {duration:.2f}s. Targets: {success_count}/{len(TARGETS)}")
        
        time.sleep(10)

if __name__ == '__main__':
    start_http_server(EXPORTER_PORT)
    main_loop()
