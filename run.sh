#!/bin/sh
# 入試合格判定アプリを起動する
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/streamlit ]; then
  echo "初回セットアップを実行します…"
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -r requirements.txt || exit 1
fi

# 初回起動時のメールアドレス入力プロンプトを出さない（空欄＝送信しない設定）
if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
  mkdir -p "$HOME/.streamlit"
  printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
fi

exec .venv/bin/streamlit run app.py
