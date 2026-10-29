"""Cover image for the Reddit AI X article (2026-10-11 追加).

作風は「1960〜70 年代のレトロなシルクスクリーンのポスター」（2026-10-12 変更）。
毎朝 X に出す 3 本の記事（Hacker News・この reddit・x-morning-brief）の表紙が
同じ Apple 風ミニマルで、読者が同じ記事と見間違えるおそれがあったため、系統ごとに
画風を分けた（Hacker News は新聞の一面、x-morning-brief は水彩）。それに伴い、
x-morning-brief の写しをやめてこのファイル単独で完結させた。

画像は 1 回生成したらそのまま使う（2026-10-12 変更、藤川さんの指示）。それまでの
「構図を上下に 2 回描く」失敗の機械判定・作り直し・陰影の後処理は外した。
不具合に気づいたら、Mac で NEWS/cover_regen.py を使って Obsidian の画像だけを
作り直す。再試行は、通信エラーなどで画像が返ってこなかったときだけ行う。

鍵は本文用の GOOGLE_API_KEY とは別の GEMINI_IMAGE_API_KEY（.env）。未設定なら画像なしで続ける。
Pillow が要る。失敗しても記事は画像なしで出る（呼び出し側で握り潰す）。
"""
from __future__ import annotations

import datetime as dt
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


def log(msg: str) -> None:
    ts = dt.datetime.now(JST).strftime("%H:%M:%S")
    print(f"[x-cover {ts}] {msg}", flush=True)


# Gemini natively tops out at 21:9, so we generate there and crop to 5:2.
# The image key is deliberately separate from GOOGLE_API_KEY (the column text)
# so the image spend can be tracked on its own bill.
BANNER_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
BANNER_KEY_ENV = "GEMINI_IMAGE_API_KEY"
BANNER_RAW_ASPECT = "21:9"  # widest ratio Gemini generates natively
BANNER_W, BANNER_H = 2560, 1024  # exactly 5:2, matching the previous banners

# 通信エラー・画像なしの応答のときだけ作り直す回数。出来の良し悪しでは作り直さない。
MAX_BANNER_ATTEMPTS = 3
BANNER_RETRY_BASE_WAIT = 3  # seconds; multiplied by the attempt number


def build_cover_prompt(article_text: str) -> str:
    """Build the image prompt from the day's Reddit AI posts (retro poster style)."""
    return (
        "以下は、海外の掲示板 Reddit で今日話題になった AI 関連の投稿の日本語要約です。\n\n"
        f"=== 投稿の要約 ===\n{article_text}\n=== ここまで ===\n\n"
        "この中から特に目を引く話題を 4 つ選び、それらを表す横長の表紙画像を作成してください。\n"
        "パッと見ただけで今日の主な話題が想像できることを最優先にしてください。\n"
        "画風は「1960〜70 年代のレトロなシルクスクリーンのポスター」です。\n"
        "- 褪せた橙・からし色・茶・ティールなど、暖色寄りの限られた色数で描く\n"
        "- 形は太く単純化し、輪郭をはっきりさせる\n"
        "- 印刷のかすれ・ざらつき・版ずれの質感を加える\n"
        "- 4 つの話題を、それぞれ 1 つの場面として描く\n"
        "画像内の文字は日本語・横書きとし、次を必ず守ってください。\n"
        "- 文字は各話題に 1 つの短いラベルだけ。最大 4 個まで\n"
        "- 各ラベルは 8 文字以内の短い語句にする。1 行で収め、語の途中で改行しない\n"
        "- 同じ語句を 2 回以上描かない\n"
        "- 文字は大きく描く。注釈・キャプション・小さな文字は描かない\n"
        "- 読めない文字・崩れた文字・意味をなさない文字列を描かない\n"
        # 21:9 で生成して 5:2 へ切り抜くので上下が少し落ちる。試作（2026-10-12）では
        # 下端のラベルが切れたので、置き場所を帯で指定する。
        "- 仕上げの工程で画像の上下の端が切り取られる。"
        "文字はすべて、上下それぞれ 15% を除いた中央の帯の中に置く\n"
        "- 大きな 1 つの見出し文字（記事タイトル）は入れない"
    )


def crop_to_banner(image_bytes: bytes) -> bytes:
    """Center-crop to 5:2 and resize. Returns PNG bytes."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    ratio = BANNER_W / BANNER_H
    if w / h > ratio:  # too wide: trim the sides
        keep = int(h * ratio)
        img = img.crop(((w - keep) // 2, 0, (w - keep) // 2 + keep, h))
    else:  # too tall: trim top and bottom
        keep = int(w / ratio)
        img = img.crop((0, (h - keep) // 2, w, (h - keep) // 2 + keep))
    img = img.resize((BANNER_W, BANNER_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def gemini_generate_banner(scene_prompt: str) -> bytes | None:
    """Call Gemini and return the first image as 5:2 PNG bytes (None on failure).

    最初に返ってきた画像をそのまま使う。作り直すのは、通信エラーや画像の
    入っていない応答のときだけ（最大 MAX_BANNER_ATTEMPTS 回）。
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get(BANNER_KEY_ENV)
    if not api_key:
        log(f"  WARNING: {BANNER_KEY_ENV} not set; skipping banner")
        return None

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_modalities=["Image"],
        image_config=types.ImageConfig(
            aspect_ratio=BANNER_RAW_ASPECT, image_size="2K"
        ),
    )

    for attempt in range(1, MAX_BANNER_ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(
                model=BANNER_MODEL, contents=scene_prompt, config=config
            )
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        meter.record_image(BANNER_MODEL, label="banner")
                        return crop_to_banner(part.inline_data.data)
            reason = "response contained no image data"
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:200]}"

        if attempt < MAX_BANNER_ATTEMPTS:
            wait = BANNER_RETRY_BASE_WAIT * attempt
            log(
                f"  WARNING: image generation attempt {attempt}/{MAX_BANNER_ATTEMPTS} "
                f"failed ({reason}); retrying in {wait}s"
            )
            time.sleep(wait)
        else:
            log(
                f"  WARNING: image generation failed after {MAX_BANNER_ATTEMPTS} "
                f"attempts ({reason})"
            )

    return None


def make_cover(article_text: str, out_path) -> bool:
    """Generate the 2560x1024 cover for the article text and write it to out_path.

    Never raises: the article must still go out without a cover.
    """
    if os.environ.get("X_COVER", "1") == "0":
        log("X_COVER=0 のためカバー画像を作りません")
        return False
    try:
        png = gemini_generate_banner(build_cover_prompt(article_text))
        if not png:
            return False
        out_path.write_bytes(png)
        log(f"cover saved: {out_path.name} ({len(png)} bytes)")
        return True
    except Exception as e:
        log(f"WARNING: カバー画像を作れませんでした: {type(e).__name__}: {str(e)[:200]}")
        return False
