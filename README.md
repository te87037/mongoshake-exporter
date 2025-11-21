# mongoshake-exporter
exporter of mongoshake
Build the main image
```
docker build -t mongoshake-exporter .
```
Docker compose config as below
```
services:
  mongoshake_exporter:
    image: mongoshake-exporter # 使用您建好的 Image 名稱
    container_name: mongoshake_exporter
    ports:
      - "9900:9900"

    environment:
      # 必選配置：設定要監控的 MongoShake 實例 (多實例用逗號分隔)
      MONGO_SHAKE_TARGETS: "uat-db=10.10.96.113:9300,qat-db=10.10.96.113:9600"

      # === 關鍵配置：設定要啟用的監控類別 ===
      # 可選值: status, latency, throughput, queue
      # 預設值為 'all' (全部開啟)

      # 範例 1: 只監控延遲和服務狀態
      MONITOR_CATEGORIES: "all"

      # 範例 2: 監控所有指標 (與預設 'all' 相同)
      # MONITOR_CATEGORIES: "latency,throughput,status,queue"

    restart: always
```
