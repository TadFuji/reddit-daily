"""毎朝の自動処理が「成功したのに中身が痩せている」状態を見つけて知らせる。

なぜこれがあるか
----------------
2026-09-26 に、Reddit の RSS 取得が毎朝 24 本中 17〜18 本も 429 で落ちていたことが
分かった。VPS へ移した 2026-07-24 から 2 か月間ずっとである。レポートは毎日できて
いたので、誰も気づかなかった。パイプラインは「異常終了したとき」だけ知らせる作りで、
「終わったが中身が減っている」ことは誰も見ていなかった。

このモジュールはその 1 点だけを担う。実行のたびに数値（取得できた取得元の数、件数、
所要時間、失敗数など）を記録し、次の 2 つの基準で異常を判定する。

  1. 下限値: 「これを下回ったらおかしい」と人が決めた値（floor）
  2. 過去との比較: 直近の実績の中央値に対して極端に小さい（ratio）

異常があれば、Obsidian へ同期されるフォルダに警告ノートを書く。毎朝の取り込みで
Mac に届くので、こちらから見に行かなくても目に入る。失敗通知（reddit-failed-*.md）
と同じ、実績のある経路である。

使い方
------
    from healthcheck import HealthCheck

    hc = HealthCheck("reddit", note_dir=REPORT_DIR, history_dir=BASE / "health")
    hc.metric("feeds_ok", 11, floor=10, of=12, label="取得できた取得元")
    hc.metric("posts", 216, floor=100, ratio=0.6, label="集めた投稿")
    hc.metric("summaries_failed", 0, ceiling=2, label="要約に失敗した件数")
    print(hc.summary_line())          # 実行ログへ 1 行
    hc.finish()                       # 記録＋異常なら警告ノート

`finish()` は `<history_dir>/health-YYYY-MM.jsonl` へ 1 行追記する（1 行 = 1 実行）。
月ごとの一覧は `python3 healthcheck.py health/` で読める。

判定の考え方
------------
- `floor`: この値を下回ったら異常（下限）。人が決める。
- `ceiling`: この値を上回ったら異常（上限）。失敗数やエラー数に使う。
- `ratio`: 直近 14 回の中央値に対する比率の下限。0.6 なら「いつもの 6 割未満で異常」。
  履歴が 5 回に満たないうちは判定しない（基準が定まらないため）。
- `of`: 分母（取得元の総数など）。記録と読みやすさのためだけに使い、判定はしない。

止め方・変え方（環境変数）
--------------------------
    HEALTHCHECK=0            判定も記録も警告ノートも出さない
    HEALTHCHECK_NOTE=0       記録は残すが警告ノートは書かない
    HEALTHCHECK_DIR=<path>   履歴の置き場所を上書きする

このモジュールは**外に例外を出さない**。健康チェックの不具合で本番の配信を落とさない
ため、内部の失敗はすべて捕捉し、理由を JSONL の `checker_errors` に残すだけにする。
ただし「静かに何もしない」のが最悪なので、捕捉した失敗は実行ログにも 1 行出す。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import statistics
from pathlib import Path

JST = _dt.timezone(_dt.timedelta(hours=9), name="JST")

# 過去との比較に使う件数。これより少ない履歴では比較しない。
HISTORY_WINDOW = 14
HISTORY_MIN = 5


class HealthCheck:
    """1 回の実行ぶんの数値を集め、基準を外れたら警告ノートを書く。"""

    def __init__(
        self,
        job: str,
        note_dir: str | os.PathLike | None = None,
        history_dir: str | os.PathLike = "health",
        note_prefix: str = "⚠️ 自動処理の異常",
        where: str | None = None,
    ) -> None:
        self.job = job
        # 警告ノートに載せる「ログの在りか」。系統ごとに違うので呼び出し側が渡す。
        self.where = where or f"VPS の `/root/{job}/cron.log`"
        self.note_dir = Path(note_dir) if note_dir else None
        self.history_dir = Path(os.environ.get("HEALTHCHECK_DIR") or history_dir)
        self.note_prefix = note_prefix
        self.started_at = _dt.datetime.now(JST)
        self.metrics: list[dict] = []
        self.problems: list[str] = []
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.enabled = os.environ.get("HEALTHCHECK", "1") != "0"
        self._history: list[dict] | None = None

    # ---- 記録と判定 -------------------------------------------------------

    def metric(
        self,
        name: str,
        value,
        floor: float | None = None,
        ceiling: float | None = None,
        ratio: float | None = None,
        of: float | None = None,
        label: str | None = None,
    ) -> None:
        """数値を 1 つ記録し、基準を外れていれば問題として控える。"""
        if not self.enabled:
            return
        try:
            entry = {"name": name, "value": value, "label": label or name}
            if of is not None:
                entry["of"] = of
            shown = f"{value}/{of}" if of is not None else f"{value}"
            title = label or name

            if floor is not None:
                entry["floor"] = floor
                if value < floor:
                    self.problems.append(f"{title}が {shown}（下限 {floor} を下回りました）")
            if ceiling is not None:
                entry["ceiling"] = ceiling
                if value > ceiling:
                    self.problems.append(f"{title}が {shown}（上限 {ceiling} を超えました）")
            if ratio is not None:
                past = self._past_values(name)
                if len(past) >= HISTORY_MIN:
                    med = statistics.median(past)
                    entry["median"] = med
                    entry["ratio"] = ratio
                    if med > 0 and value < med * ratio:
                        self.problems.append(
                            f"{title}が {shown}。直近 {len(past)} 回の中央値 {med:g} の"
                            f"{value / med:.0%}しかありません"
                        )
            self.metrics.append(entry)
        except Exception as exc:
            self._note_error(f"metric({name}) 失敗: {type(exc).__name__}: {exc}")

    def problem(self, text: str) -> None:
        """数値にならない異常（想定外の状態など）を直接足す。"""
        if self.enabled:
            self.problems.append(text)

    def note(self, text: str) -> None:
        """警告ノートに添える補足。異常の有無は変えない。"""
        if self.enabled:
            self.notes.append(text)

    # ---- 履歴 -------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        if self._history is not None:
            return self._history
        rows: list[dict] = []
        try:
            files = sorted(self.history_dir.glob("health-*.jsonl"))[-2:]
            for path in files:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("job") == self.job:
                        rows.append(row)
        except Exception as exc:
            self._note_error(f"履歴の読み込み失敗: {type(exc).__name__}: {exc}")
        self._history = rows[-HISTORY_WINDOW:]
        return self._history

    def _past_values(self, name: str) -> list[float]:
        out = []
        for row in self._load_history():
            for m in row.get("metrics", []):
                if m.get("name") == name and isinstance(m.get("value"), (int, float)):
                    out.append(float(m["value"]))
        return out

    # ---- 出力 -------------------------------------------------------------

    def summary_line(self) -> str:
        """実行ログに 1 行残す用。異常が無いときも必ず出す（生存の証拠になる）。"""
        try:
            if not self.enabled:
                return "HEALTH: 無効（HEALTHCHECK=0）"
            parts = [f"{m['label']}={m['value']}" + (f"/{m['of']}" if "of" in m else "")
                     for m in self.metrics]
            head = "HEALTH: 異常なし" if not self.problems else f"HEALTH: 異常 {len(self.problems)} 件"
            body = " / ".join(parts) if parts else "計測なし"
            tail = f" / チェッカーのエラー{len(self.errors)}件" if self.errors else ""
            return f"{head} / {body}{tail}"
        except Exception as exc:
            return f"HEALTH: 集計に失敗（{type(exc).__name__}: {exc}）"

    def finish(self, extra: dict | None = None) -> Path | None:
        """履歴へ 1 行追記し、異常があれば警告ノートを書く。戻り値はノートのパス。"""
        if not self.enabled:
            return None
        row = {
            "job": self.job,
            "at": self.started_at.isoformat(timespec="seconds"),
            "date": self.started_at.strftime("%Y-%m-%d"),
            "problems": self.problems,
            "metrics": self.metrics,
        }
        if extra:
            row["extra"] = extra
        if self.errors:
            row["checker_errors"] = self.errors
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            path = self.history_dir / f"health-{self.started_at:%Y-%m}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._note_error(f"履歴の書き出し失敗: {type(exc).__name__}: {exc}")
        if not self.problems:
            # 同じ日の前の試行が警告を残していたら取り消す。再試行で持ち直した朝に
            # 古い警告が残ると、読む人が「今日も駄目だった」と受け取ってしまう。
            self._clear_note()
            return None
        return self._write_note()

    def _clear_note(self) -> None:
        if self.note_dir is None:
            return
        try:
            old = self.note_dir / (
                f"{self.note_prefix}-{self.job}-{self.started_at:%Y-%m-%d}.md"
            )
            if old.exists():
                old.unlink()
                print(f"[health] 前の試行の警告ノートを取り消しました: {old.name}", flush=True)
        except Exception as exc:
            self._note_error(f"古い警告ノートを消せませんでした: {type(exc).__name__}: {exc}")

    def _write_note(self) -> Path | None:
        if os.environ.get("HEALTHCHECK_NOTE", "1") == "0" or self.note_dir is None:
            return None
        try:
            date_str = self.started_at.strftime("%Y-%m-%d")
            lines = [
                "---",
                "tags: [自動処理, 異常検知]",
                f"date: {date_str}",
                f"job: {self.job}",
                "---",
                "",
                f"# ⚠️ {self.job} の自動処理に異常があります（{date_str}）",
                "",
                "処理そのものは最後まで走りましたが、いつもと違う数値が出ています。",
                "レポートは届いていても、中身が痩せている可能性があります。",
                "",
                "## 見つかった問題",
                "",
            ]
            lines += [f"{i}. {p}" for i, p in enumerate(self.problems, 1)]
            lines += ["", "## 今回の数値", ""]
            for m in self.metrics:
                shown = f"{m['value']}" + (f" / {m['of']}" if "of" in m else "")
                extra = []
                if "floor" in m:
                    extra.append(f"下限 {m['floor']}")
                if "ceiling" in m:
                    extra.append(f"上限 {m['ceiling']}")
                if "median" in m:
                    extra.append(f"直近の中央値 {m['median']:g}")
                suffix = f"（{'、'.join(extra)}）" if extra else ""
                lines.append(f"- {m['label']}: {shown}{suffix}")
            if self.notes:
                lines += ["", "## 補足", ""] + [f"- {n}" for n in self.notes]
            if self.errors:
                lines += ["", "## チェッカー自身のエラー", ""] + [f"- {e}" for e in self.errors]
            lines += [
                "",
                "## 次にどうするか",
                "",
                "Claude Code を開いて、このノートの名前を伝えて調べてもらってください。",
                f"実行ログは{self.where}、数値の履歴は `health/health-YYYY-MM.jsonl` にあります。",
                "",
                "この警告が毎日出て邪魔な場合は、基準値が現実に合っていない可能性があります。",
                "基準はコードの中（`healthcheck` の `floor` / `ratio` の指定）にあります。",
            ]
            self.note_dir.mkdir(parents=True, exist_ok=True)
            out = self.note_dir / f"{self.note_prefix}-{self.job}-{date_str}.md"
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(out)
            return out
        except Exception as exc:
            self._note_error(f"警告ノートの書き出し失敗: {type(exc).__name__}: {exc}")
            return None

    def _note_error(self, msg: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(msg)
        # 黙って失われないよう、実行ログにも出す
        print(f"[health] WARNING: {msg}", flush=True)


# ---- 月次の読み出し（python3 healthcheck.py health/） ------------------------


def _report(log_dir: str) -> int:
    d = Path(log_dir)
    files = sorted(d.glob("health-*.jsonl")) if d.is_dir() else []
    if not files:
        print(f"記録が見つかりません: {d}")
        return 1
    for path in files:
        print(f"\n{path.name}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mark = "異常" if row.get("problems") else "  OK"
            nums = " ".join(
                f"{m['label']}={m['value']}" + (f"/{m['of']}" if "of" in m else "")
                for m in row.get("metrics", [])
            )
            print(f"  {mark} {row.get('date')} {row.get('job')}  {nums}")
            for p in row.get("problems", []):
                print(f"       - {p}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_report(sys.argv[1] if len(sys.argv) > 1 else "health"))
