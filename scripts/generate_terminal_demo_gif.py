from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "deliverables"
OUT_GIF = OUT_DIR / "blocksize_agent_batch_demo.gif"
OUT_POSTER = OUT_DIR / "blocksize_agent_batch_demo_poster.png"

WIDTH = 1400
HEIGHT = 900
PADDING = 42
TERMINAL_RADIUS = 24
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 25
SMALL_FONT_SIZE = 18
HEADER_FONT_SIZE = 26
LINE_HEIGHT = 34
SCROLL_TOP = 154
SCROLL_BOTTOM = HEIGHT - 72

BG_TOP = (6, 10, 18)
BG_BOTTOM = (11, 20, 34)
TERMINAL_BG = (8, 13, 21)
TERMINAL_BORDER = (33, 50, 76)
TEXT_MAIN = (216, 233, 242)
TEXT_MUTED = (135, 166, 186)
TEXT_GREEN = (76, 224, 127)
TEXT_MONEY = (255, 206, 89)
TEXT_ALERT = (255, 115, 115)
TEXT_LINK = (118, 170, 255)
TEXT_PROMPT = (111, 217, 151)
TEXT_CURSOR = (230, 239, 247)


TRANSCRIPT_LINES: list[tuple[str, str]] = [
    ("✅ Agent batch run complete.", "green"),
    (
        "(Agentic Payments) user@anonymous-macbook Agentic Payments % python "
        "scripts/autonomous_solana_agent.py https://agentic-payments-production.up.railway.app",
        "prompt",
    ),
    ("", "main"),
    ("🤖 [Agent]: Identity loaded securely from Environment", "green"),
    ("", "main"),
    ("============================================================", "muted"),
    ("🟢 AUTONOMOUS AGENT WALLET STATUS", "green"),
    ("============================================================", "muted"),
    ("Agent Wallet Address: 49FF6NWognLXj751gV6hzD4wYiXFA8SqmFhnALaJc5mQ", "main"),
    ("SOL Balance:  0.029880017 SOL (gas)", "main"),
    ("USDC Balance: 0.059626 USDC", "main"),
    ("============================================================", "muted"),
    ("", "main"),
    (
        "🤖 [Agent]: Budget secured. Contacting Discovery Node for Master Instrument List...",
        "main",
    ),
    ("", "main"),
    (
        "🤖 [Agent]: Assembling BATCH payload containing 10 assets across all asset classes...",
        "main",
    ),
    ("", "main"),
    (
        "[*] Requesting BATCH endpoint: https://agentic-payments-production.up.railway.app/"
        "v1/batch?reqs=bidask:AVAXUSD,vwap:ETHUSD,bidask:SOLUSD,bidask:SOLUSD,bidask:BTCUSD,"
        "bidask:LINKUSD,equity:AAPL,fx:EURUSD,metal:XAUUSD",
        "link",
    ),
    ("   ⛔ Intercepted 402 Payment Required for entire batch.", "alert"),
    ("   💰 Total Due dynamically calculated: $0.0390 USDC", "money"),
    (
        "   💸 Executing SOLANA Native USDC Transfer: $0.039 to "
        "89xvyfLae5s1BgWBmdrsWQfJ3fYDQFywYBJev8fgtMEu",
        "money",
    ),
    (
        "   ⏳ Broadcasted TX: 4k2PffdMqdpCkhR6P66AD8JG4RhcNKqtPuG5bcR5ivpYyMB1y1oEie2NmEKWSg"
        "vezpgQ5FPcMgMoa1Wfn5mLo1H4 (waiting for confirmation...)",
        "main",
    ),
    ("   ✅ Transaction Confirmed!", "green"),
    ("   [*] Resubmitting BATCH request with cryptographic signature...", "main"),
    ("   🎉 SUCCESS! Huge Multi-Asset Payload Retrieved:", "green"),
    ("{", "main"),
    ('  "status": "ok",', "main"),
    ('  "batch_size": 9,', "main"),
    ('  "results": [', "main"),
    ("    {", "main"),
    ('      "request": "bidask:AVAXUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "crypto", '
        '"bid": 9.2214, "ask": 9.2313, "spread_pct": 0.1077 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "vwap:ETHUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "crypto", '
        '"vwap": 2315.8964, "volume": 2155.4426 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "bidask:BTCUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "crypto", '
        '"bid": 75688.0362, "ask": 75688.1021 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "bidask:LINKUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "crypto", '
        '"bid": 9.2721, "ask": 9.2794 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "equity:AAPL",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "equity", '
        '"ticker": "AAPL", "last": 181.52, "bid": 181.40, "ask": 181.60 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "fx:EURUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "fx", '
        '"bid": 1.178685, "ask": 1.178787, "mid": 1.178736 }',
        "main",
    ),
    ("    },", "main"),
    ("    {", "main"),
    ('      "request": "metal:XAUUSD",', "main"),
    (
        '      "response": { "status": "ok", "asset_class": "metal", '
        '"name": "Gold", "price": 4809.55 }',
        "main",
    ),
    ("    },", "main"),
    ("    }", "main"),
    ("  ]", "main"),
    ("}", "main"),
    ("", "main"),
    ("✅ Agent batch run complete.", "green"),
    ("(Agentic Payments) user@anonymous-macbook Agentic Payments % ", "prompt"),
]


def make_gradient() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    px = image.load()
    for y in range(HEIGHT):
        t = y / max(HEIGHT - 1, 1)
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        for x in range(WIDTH):
            glow = 0.0
            dx = (x - WIDTH * 0.78) / WIDTH
            dy = (y - HEIGHT * 0.2) / HEIGHT
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 0.32:
                glow = (0.32 - dist) / 0.32
            rr = min(255, int(r + 30 * glow))
            gg = min(255, int(g + 45 * glow))
            bb = min(255, int(b + 80 * glow))
            px[x, y] = (rr, gg, bb)
    return image


def wrap_line(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    if not text:
        return [""]
    words = text.split(" ")
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def build_render_lines(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, inner_width: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for text, style in TRANSCRIPT_LINES:
        for line in wrap_line(draw, text, font, inner_width):
            out.append((line, style))
    return out


def style_color(style: str) -> tuple[int, int, int]:
    return {
        "main": TEXT_MAIN,
        "muted": TEXT_MUTED,
        "green": TEXT_GREEN,
        "money": TEXT_MONEY,
        "alert": TEXT_ALERT,
        "link": TEXT_LINK,
        "prompt": TEXT_PROMPT,
    }.get(style, TEXT_MAIN)


def draw_window(base: Image.Image) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = base.copy()
    draw = ImageDraw.Draw(img)
    x0 = PADDING
    y0 = PADDING
    x1 = WIDTH - PADDING
    y1 = HEIGHT - PADDING
    draw.rounded_rectangle((x0, y0, x1, y1), radius=TERMINAL_RADIUS, fill=TERMINAL_BG, outline=TERMINAL_BORDER, width=2)
    draw.rounded_rectangle((x0, y0, x1, y0 + 54), radius=TERMINAL_RADIUS, fill=(14, 21, 33))
    draw.rectangle((x0, y0 + 28, x1, y0 + 54), fill=(14, 21, 33))
    for idx, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = x0 + 24 + idx * 20
        cy = y0 + 27
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=color)
    return img, draw


def render_frame(base_bg: Image.Image, lines: list[tuple[str, str]], upto: int, hold_cursor: bool) -> Image.Image:
    img, draw = draw_window(base_bg)
    header_font = ImageFont.truetype(FONT_PATH, HEADER_FONT_SIZE)
    mono_font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    small_font = ImageFont.truetype(FONT_PATH, SMALL_FONT_SIZE)

    draw.text((PADDING + 22, 82), "Blocksize Agentic Payments", fill=TEXT_MAIN, font=header_font)
    draw.text(
        (PADDING + 22, 114),
        "Autonomous wallet -> 402 challenge -> native USDC payment -> multi-asset data batch",
        fill=TEXT_MUTED,
        font=small_font,
    )

    visible_height = SCROLL_BOTTOM - SCROLL_TOP
    full_height = max(len(lines), 1) * LINE_HEIGHT
    max_scroll = max(0, full_height - visible_height)
    current_scroll = min(max_scroll, max(0, upto * LINE_HEIGHT - visible_height + 5 * LINE_HEIGHT))

    x = PADDING + 24
    y = SCROLL_TOP - current_scroll
    cursor_xy: tuple[int, int] | None = None
    for idx, (text, style) in enumerate(lines[:upto]):
        if SCROLL_TOP - LINE_HEIGHT <= y <= SCROLL_BOTTOM:
            draw.text((x, y), text, fill=style_color(style), font=mono_font)
        if idx == upto - 1:
            cursor_xy = (x + int(draw.textlength(text, font=mono_font)), y)
        y += LINE_HEIGHT

    if hold_cursor and cursor_xy is not None and SCROLL_TOP - LINE_HEIGHT <= cursor_xy[1] <= SCROLL_BOTTOM:
        draw.rectangle((cursor_xy[0] + 3, cursor_xy[1] + 5, cursor_xy[0] + 17, cursor_xy[1] + 31), fill=TEXT_CURSOR)

    bar_x0 = WIDTH - PADDING - 18
    bar_y0 = SCROLL_TOP
    bar_y1 = SCROLL_BOTTOM
    draw.rounded_rectangle((bar_x0, bar_y0, bar_x0 + 6, bar_y1), radius=3, fill=(25, 38, 56))
    if max_scroll:
        thumb_h = max(90, int(visible_height * visible_height / full_height))
        thumb_y = bar_y0 + int((visible_height - thumb_h) * current_scroll / max_scroll)
    else:
        thumb_h = visible_height
        thumb_y = bar_y0
    draw.rounded_rectangle((bar_x0, thumb_y, bar_x0 + 6, thumb_y + thumb_h), radius=3, fill=(76, 224, 127))
    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bg = make_gradient()
    _, prep_draw = draw_window(bg)
    mono_font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    inner_width = WIDTH - (PADDING * 2) - 60
    render_lines = build_render_lines(prep_draw, mono_font, inner_width)

    frames: list[Image.Image] = []
    durations: list[int] = []

    checkpoints = list(range(1, len(render_lines) + 1))
    for idx, upto in enumerate(checkpoints):
        hold = idx % 3 != 1
        frames.append(render_frame(bg, render_lines, upto, hold_cursor=hold))
        durations.append(110)

    for _ in range(10):
        frames.append(render_frame(bg, render_lines, len(render_lines), hold_cursor=True))
        durations.append(140)

    frames[0].save(
        OUT_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
    )
    frames[-1].save(OUT_POSTER)
    print(OUT_GIF)
    print(OUT_POSTER)


if __name__ == "__main__":
    main()
