#!/bin/bash
# ダブルクリックで操作画面を開くためのランチャー。
cd "$(dirname "$0")" || exit 1

if [ ! -x venv/bin/streamlit ]; then
  echo "まだ準備ができていません。"
  echo "同じフォルダにある「準備.command」を先にダブルクリックしてください。"
  echo
  read -r -p "Enterキーで閉じます..."
  exit 1
fi

echo "操作画面を準備しています。ブラウザが自動で開きます..."
echo "終わるときは、このウィンドウで Control + C を押すか、ウィンドウを閉じてください。"
echo

venv/bin/streamlit run app.py &
PID=$!

# サーバーが立ち上がるのを待ってからブラウザを開く
for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://localhost:8501"; then break; fi
  sleep 0.5
done
open "http://localhost:8501"

wait $PID
