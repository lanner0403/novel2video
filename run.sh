#!/usr/bin/env bash
# 一鍵啟動。venv 建不起來會自動退回直接安裝，盡量在各環境都能跑。
set -e
cd "$(dirname "$0")/backend"

# 偵測 python 指令
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "找不到 python，請先安裝 Python 3.10+"; exit 1
fi
echo "使用 $($PY --version)"

# 嘗試建立 venv，失敗就跳過用系統環境
USE_VENV=1
if [ ! -d ".venv" ]; then
  $PY -m venv .venv 2>/dev/null || USE_VENV=0
fi
if [ "$USE_VENV" = "1" ] && [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
  PY="python"
else
  echo "（未使用 venv，改裝到系統環境）"
fi

# 安裝相依，相容新版 pip 的外部管理限制
$PY -m pip install -q -r requirements.txt \
  || $PY -m pip install -q -r requirements.txt --break-system-packages \
  || $PY -m pip install -q -r requirements.txt --user

# 載入 .env（若存在）
if [ -f "../.env" ]; then set -a; source ../.env; set +a; fi

echo "▶ 服務啟動於 http://127.0.0.1:8000  （Ctrl+C 結束）"
exec $PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
