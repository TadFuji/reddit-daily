# reddit-daily

Reddit の話題を日本語でまとめる 3 本のレポート（標準・AI・セミナー）と、note の「Reddit 日本語解説」の原稿・表紙を、毎朝 GitHub Actions で作るリポジトリです。

- 起動：時刻係（Cloudflare の `news-clock`）が毎朝 6:00 ごろに `reddit-generate.yml` を起動します。手で起動することもできます。
- 公開：できた note 原稿を `TadFuji/reddit-note-drop` に置きます。note への公開は、時刻係が別のワークフローを起動して行います。
- 保管：できたファイルは非公開の `TadFuji/news-outbox` に置き、Mac が起きているときに Obsidian へ取り込みます。
- 試験：`reddit-trial.yml` を起動すると、受け渡しをせず、保管先も `reddit-trial/` に分けます。本番の記録とは別のワークフローとして残るので、時刻係の判断に混ざりません。

鍵（GitHub の Secrets）：`GEMINI_API_KEY`、`GEMINI_IMAGE_API_KEY`、`DROP_TOKEN`（reddit-note-drop の書き込み）、`OUTBOX_TOKEN`（news-outbox の書き込み）。

コードは 2026-10-25〜29 に Grok Bot の box で本番に使っていた版を元にしています。
