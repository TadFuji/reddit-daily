"""Cover image for the Reddit daily-trend note.com article (2026-10-12 追加).

作風は「1960〜70 年代のレトロなシルクスクリーンのポスター」（2026-10-29 藤川さんが決定。
それまではドット絵で、見本 10 種 ../表紙画風サンプル-20261012/ から選んでいた）。Hacker News の note の表紙
（Apple 風）とも、X 用の 3 種（新聞の一面・レトロポスター・水彩）とも違う画風にして、
同じ記事と見間違えられないようにしている。

画像は 1 回生成したらそのまま使う（NEWS 共通の決まり）。作り直すのは、通信エラーなどで
画像が返らなかったときだけ。複製の機械判定や陰影の後処理は持たない。

note.com の表紙は 1280×670。Gemini は 16:9 で生成し、中央を切り抜いて縮める
（Hacker News の cover_image.py と同じ大きさ）。鍵は x_cover.py と同じ GEMINI_IMAGE_API_KEY。
止めるときは環境変数 NOTE_COVER=0。失敗しても例外を出さない（呼び出し側は画像なしで続ける）。
"""
from __future__ import annotations

import datetime as dt
import io
import os
import time

try:
    from usage_meter import meter
except ImportError:  # VPS への転送漏れで本番を止めない
    class _NullMeter:
        def __getattr__(self, _):
            return lambda *a, **k: None

    meter = _NullMeter()

JST = dt.timezone(dt.timedelta(hours=9), name="JST")

COVER_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
COVER_KEY_ENV = "GEMINI_IMAGE_API_KEY"
COVER_RAW_ASPECT = "16:9"  # 1280x670 (約 1.91:1) に最も近いネイティブ比率
TARGET_W, TARGET_H = 1280, 670

# 通信エラー・画像なしの応答のときだけ作り直す回数。出来の良し悪しでは作り直さない。
MAX_ATTEMPTS = 3
RETRY_BASE_WAIT = 3  # seconds; multiplied by the attempt number


def log(msg: str) -> None:
    ts = dt.datetime.now(JST).strftime("%H:%M:%S")
    print(f"[note-cover {ts}] {msg}", flush=True)


def build_prompt(article_text: str) -> str:
    """Build the image prompt from the day's Reddit posts (retro poster style)."""
    return (
        "以下は、海外の掲示板 Reddit で今日話題になった投稿の日本語要約です。\n\n"
        f"=== 投稿の要約 ===\n{article_text}\n=== ここまで ===\n\n"
        "この中から特に目を引く話題を 4 つ選び、それらを表す横長の表紙画像を作成してください。\n"
        "パッと見ただけで今日の主な話題が想像できることを最優先にしてください。\n"
        "画風は「1960〜70 年代のレトロなシルクスクリーンのポスター」です。\n"
        "- 画面を 2×2 の 4 区画に分け、各区画に 1 つの話題を 1 つの場面として描く\n"
        "- 褪せた橙・からし色・茶・ティールなど、暖色寄りの限られた色数で描く\n"
        "- 形は太く単純化し、輪郭をはっきりさせる\n"
        "- 印刷のかすれ・ざらつき・版ずれの質感を加える\n"
        "- 性的・暴力的・残酷な表現、実在の人物の似顔絵、企業のロゴは描かない\n"
        "画像内の文字は日本語・横書きとし、次を必ず守ってください。\n"
        "- 文字は各区画に 1 つの短いラベルだけ。最大 4 個まで\n"
        "- 各ラベルは 8 文字以内の短い語句にする。1 行で収め、語の途中で改行しない\n"
        "- ラベルは区画の下寄りに、無地の帯の上に置く\n"
        "- 同じ語句を 2 回以上描かない\n"
        "- 文字は大きく描く。注釈・キャプション・小さな文字は描かない\n"
        "- 読めない文字・崩れた文字・意味をなさない文字列を描かない\n"
        # 16:9 で生成して 1280x670 へ切り抜くとき、上端だけを約 7% 削る（crop_to_cover）。
        # 2026-10-12 の試作では上下を均等に削り、下の段のラベルが切れた。
        "- 仕上げの工程で画像の上端が少し切り取られる。上端の近くには文字や大事な物を置かない\n"
        "- 大きな 1 つの見出し文字（記事タイトル）は入れない"
    )


def crop_to_cover(image_bytes: bytes) -> bytes:
    """Crop to 1280:670 and resize. Returns PNG bytes.

    When the image is too tall, only the TOP is trimmed: the labels sit at the bottom of
    each panel, and trimming both edges cut the bottom row's labels in the 2026-10-12 trial.
    NEAREST keeps the pixel-art edges crisp; LANCZOS would blur the dots.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    ratio = TARGET_W / TARGET_H
    if w / h > ratio:  # too wide: trim the sides
        keep = int(h * ratio)
        img = img.crop(((w - keep) // 2, 0, (w - keep) // 2 + keep, h))
    else:  # too tall: trim the top only (labels live at the bottom)
        keep = int(w / ratio)
        img = img.crop((0, h - keep, w, h))
    img = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def generate(prompt: str) -> bytes | None:
    """Call Gemini and return the first image as 1280x670 PNG bytes (None on failure)."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get(COVER_KEY_ENV)
    if not api_key:
        log(f"WARNING: {COVER_KEY_ENV} が未設定のため表紙を作りません")
        return None

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_modalities=["Image"],
        image_config=types.ImageConfig(aspect_ratio=COVER_RAW_ASPECT, image_size="2K"),
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(model=COVER_MODEL, contents=prompt, config=config)
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        meter.record_image(COVER_MODEL, label="note-cover")
                        return crop_to_cover(part.inline_data.data)
            reason = "response contained no image data"
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:200]}"
        if attempt < MAX_ATTEMPTS:
            wait = RETRY_BASE_WAIT * attempt
            log(f"WARNING: 表紙の生成 {attempt}/{MAX_ATTEMPTS} 回目に失敗（{reason}）。{wait} 秒後に再試行")
            time.sleep(wait)
        else:
            log(f"WARNING: 表紙を {MAX_ATTEMPTS} 回試して作れませんでした（{reason}）")
    return None


def make_cover(article_text: str, out_path) -> bool:
    """Generate the 1280x670 note cover and write it to out_path. Never raises."""
    if os.environ.get("NOTE_COVER", "1") == "0":
        log("NOTE_COVER=0 のため表紙を作りません")
        return False
    try:
        png = generate(build_prompt(article_text))
        if not png:
            return False
        out_path.write_bytes(png)
        log(f"cover saved: {out_path.name} ({len(png)} bytes)")
        return True
    except Exception as e:
        log(f"WARNING: 表紙を作れませんでした: {type(e).__name__}: {str(e)[:200]}")
        return False
