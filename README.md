# reddit-daily

Reddit の AI 関連コミュニティの話題を日本語でまとめるレポート一式（Obsidian 用 AI レポート・X 用記事/表紙・note 用原稿/表紙）を、毎朝 GitHub Actions で生成するリポジトリです。

## 仕組み
- **起動**：時刻係（Cloudflare Worker `news-clock`）が毎朝 6:00 JST ごろに `reddit-generate.yml` を起動します（手動実行も可）。
- **データ取得（`fetch_data.py`）**：無料の公開アーカイブ **Arctic Shift API**（`arctic-shift.photon-reddit.com`）から `config_ai.json` の 14 サブレディット（直近 48 時間）と選定投稿の上位コメントを取得します（API キー・Google Search Grounding 不要、取得費用 `¥0`）。
- **要約・記事生成（`run_reddit_gemini.py` / `assemble_ai_report_gemini.py`）**：Gemini API（`gemini-4-hydrogen` / `gemini-nano-banana-2.1`）で日本語要約・編集レビュー・表紙画像を生成します。
- **公開・保管**：note 原稿と表紙を `TadFuji/reddit-note-drop` に渡し、`TadFuji/news-outbox`（`reddit/`）へ保管して Mac 側で Obsidian に取り込みます。
- **試験実行（`reddit-trial.yml`）**：`note` への受け渡しを行わず、保管先も `reddit-trial/` に分けて動作検証できます。

## テストとロールバック
```bash
# オフラインテスト（API キー・通信不要）
.venv/bin/python test_fetch_data.py
.venv/bin/python test_push_note_github.py
.venv/bin/python test_note_article.py
.venv/bin/python test_x_article.py
.venv/bin/python test_healthcheck.py
.venv/bin/python test_output_paths.py
.venv/bin/python tools/test_outbox.py

# 本番反映を元に戻す場合
git revert <commit_sha>
```

鍵（GitHub Secrets）：`GEMINI_API_KEY`、`GEMINI_IMAGE_API_KEY`、`DROP_TOKEN`（`reddit-note-drop` 書き込み）、`OUTBOX_TOKEN`（`news-outbox` 書き込み）。
