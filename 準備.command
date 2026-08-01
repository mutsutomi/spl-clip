#!/bin/bash
# 初回だけ実行する準備スクリプト。ダブルクリックで動く。
cd "$(dirname "$0")" || exit 1

FFMPEG_BASE="https://ffmpeg.martin-riedl.de/redirect/latest"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1"; echo; read -r -p "Enterキーで閉じます..."; exit 1; }

say "spl-clip の準備を始めます(初回だけ・数分かかります)"

# ---- 1. Python を探す ----------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" > /dev/null 2>&1 || continue
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2> /dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  fail "Python 3.10 以降が見つかりませんでした。
https://www.python.org/downloads/ から最新版をインストールしたあと、
もう一度この「準備」をダブルクリックしてください。"
fi
echo "  使う Python: $("$PY" -V 2>&1)"

# ---- 2. 専用の環境を作る -------------------------------------------------
say "1/3 専用の環境を作っています..."
if [ -x venv/bin/python3 ]; then
  echo "  すでにあるものを使います"
else
  "$PY" -m venv venv || fail "環境の作成に失敗しました。"
fi

# ---- 3. ライブラリを入れる -----------------------------------------------
say "2/3 必要なライブラリを入れています(数分かかることがあります)..."
venv/bin/python3 -m pip install --quiet --upgrade pip 2> /dev/null
venv/bin/python3 -m pip install --quiet numpy streamlit || fail "ライブラリの導入に失敗しました。インターネット接続を確認してください。"
echo "  完了"

# ---- 4. ffmpeg を用意する ------------------------------------------------
say "3/3 動画処理ツール(ffmpeg)を用意しています..."
if [ -x bin/ffmpeg ] && [ -x bin/ffprobe ]; then
  echo "  すでにこのフォルダにあります"
elif command -v ffmpeg > /dev/null 2>&1 && command -v ffprobe > /dev/null 2>&1; then
  echo "  パソコンに入っているものを使います($(command -v ffmpeg))"
else
  case "$(uname -m)" in
    arm64) PLAT="macos/arm64" ;;   # Apple シリコン
    *)     PLAT="macos/amd64" ;;   # Intel
  esac
  mkdir -p bin
  for n in ffmpeg ffprobe; do
    [ -x "bin/$n" ] && continue
    echo "  $n をダウンロードしています..."
    curl -fL --progress-bar -o "bin/$n.zip" "$FFMPEG_BASE/$PLAT/release/$n.zip" \
      || fail "$n のダウンロードに失敗しました。インターネット接続を確認してください。"
    unzip -o -q "bin/$n.zip" -d bin || fail "$n の展開に失敗しました。"
    rm -f "bin/$n.zip"
    chmod +x "bin/$n"
    xattr -d com.apple.quarantine "bin/$n" 2> /dev/null || true
  done
  echo "  完了"
fi

say "準備ができました!"
echo "これからは「起動.command」をダブルクリックすれば使えます。"
echo
read -r -p "Enterキーで閉じます..."
