"""生成 README 首屏的演示 GIF。

逐帧渲染一个对话框风格的动画：用户提问 → 调用 MCP 工具 → 输出积分报告。
不依赖录屏，纯 Pillow 绘制，结果可复现。

用法：
    python scripts/make_demo_gif.py                 # 输出 assets/demo.gif
    python scripts/make_demo_gif.py --out x.gif     # 指定输出路径
    python scripts/make_demo_gif.py --frames        # 另存每帧 PNG 便于检查

说明：GIF 中的积分账户数值为演示数据，商品名称与剔除规模来自真实接口返回。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 画布
W, H = 900, 334
PAD = 36

# 配色：麦当劳品牌金 + GitHub 深色底，保证在 README 深浅两种主题下都清晰
BG = (13, 17, 23)
BAR = (22, 27, 34)
BORDER = (48, 54, 61)
GOLD = (255, 188, 13)
TEXT = (230, 237, 243)
DIM = (125, 133, 144)
GREEN = (63, 185, 80)
RED = (248, 81, 73)

FONT_DIR = Path("C:/Windows/Fonts")


def load_fonts() -> dict[str, ImageFont.FreeTypeFont]:
    """加载中文字体。微软雅黑是 ttc 字体集合，需指定 index。"""
    yahei = FONT_DIR / "msyh.ttc"
    if not yahei.exists():  # 兜底到黑体
        yahei = FONT_DIR / "simhei.ttf"

    def f(size: int, index: int = 0) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(str(yahei), size, index=index)
        except Exception:
            return ImageFont.truetype(str(FONT_DIR / "simhei.ttf"), size)

    return {
        "title": f(15),
        "body": f(15),
        "head": f(16),
        "small": f(13),
        "table": f(14),
    }


# 表格列的绝对 x 坐标。中文字体非等宽，靠绝对定位保证对齐
COLS = (PAD + 8, 400, 500)


def _row(cells: tuple[str, ...], color=TEXT, font_key="table"):
    return {"kind": "row", "cells": cells, "color": color, "font": font_key}


# 动画内容。每个 item 渲染为一帧，按顺序累积出现。
#
# 内容与 rank_redemptions.py --demo 的输出保持一致，改动时两边同步。
# 演示账户为构造值：可用 3280 / 本月将过期 400。
# 过期额度取 400 是为了让三档推荐都有内容：400 内装下 50+50+100，
# 500 分落入略超档（上限 600），800 分落入券面档。
#
# 输出只有两块：一句积分体检带行动召唤，加一张三列清单。
#
# 刻意不展示的内容：
# 1. 被过滤掉的商品统计。用户对全量兑换清单没有概念，只需知道自己能兑什么。
# 2. 历史已过期积分。那是无法挽回的损失，摆出来只会引发损失厌恶情绪，
#    读起来像在指责用户，而不是帮他解决问题。
# 3. 缺口多少、怎么补、覆盖率百分比。都是内部算账口径，
#    「本月将过期 400 分」本身已经带足紧迫感，后面接行动召唤就够了。
# 4. 推荐理由。三档的排序顺序本身已经承载推荐逻辑：先能用掉过期分的，
#    再略微多花的，最后券面档位更高的。标出理由反而增加阅读负担。
# 5. 调用了哪些 MCP 工具。那是给开发者看的，README 里讲，不占演示画面。
ITEMS: list[dict] = [
    {"kind": "kv", "parts": [("积分体检：可用 ", DIM), ("3280", GOLD),
                             (" 分，本月将过期 ", DIM), ("400", GOLD),
                             (" 分。", DIM), ("快看看有哪些优惠券能兑换吧", TEXT)]},
    {"kind": "gap", "h": 8},

    _row(("商品", "积分", "兑换截止"), color=DIM, font_key="small"),
    {"kind": "line"},
    _row(("6.9元可乐麦炫酷", "50", "2026-12-30")),
    _row(("6.9元香芋派/菠萝派任选", "50", "2026-12-30")),
    _row(("10.9元指定小食任选", "100", "2026-12-30")),
    _row(("16.9元麦辣可乐组合", "500", "2026-12-30")),
    _row(("17.9元板烧可乐组合", "500", "2026-12-30")),
    _row(("21.9元巨无霸可乐组合", "800", "2026-12-30")),
]

QUERY = "我的麦当劳积分快过期了吗"
TYPE_STEP = 2  # 每帧打出几个字


def draw_base(fonts: dict) -> Image.Image:
    """画静态部分：窗口边框、标题栏、提示符。"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W - 1, 38], fill=BAR)
    d.line([0, 38, W, 38], fill=BORDER)
    for i, c in enumerate(((248, 81, 73), (255, 188, 13), (63, 185, 80))):
        d.ellipse([PAD // 2 + i * 18, 14, PAD // 2 + i * 18 + 10, 24], fill=c)
    d.text((PAD + 52, 11), "mcd-points-guardian", font=fonts["small"], fill=DIM)

    # 不画底部声明栏。"演示数据"由 README 的图注说明，"只读不交易"属于
    # 安全章节的内容，放在首屏演示里是自我辩护，占地方且不是用户此刻关心的事。
    return img


def render(fonts: dict, typed: str, shown: int, cursor: bool) -> Image.Image:
    img = draw_base(fonts)
    d = ImageDraw.Draw(img)

    # 用户输入行
    y = 62
    d.text((PAD, y), ">", font=fonts["head"], fill=GOLD)
    d.text((PAD + 20, y), typed, font=fonts["head"], fill=TEXT)
    if cursor:
        cx = PAD + 22 + d.textlength(typed, font=fonts["head"])
        d.rectangle([cx, y + 2, cx + 8, y + 18], fill=GOLD)

    y = 100
    for item in ITEMS[:shown]:
        kind = item["kind"]
        if kind == "gap":
            y += item["h"]
        elif kind == "tool":
            d.text((PAD, y), "·", font=fonts["small"], fill=GREEN)
            d.text((PAD + 12, y), item["text"], font=fonts["small"], fill=DIM)
            y += 22
        elif kind == "head":
            d.rectangle([PAD, y + 3, PAD + 3, y + 16], fill=GOLD)
            d.text((PAD + 12, y), item["text"], font=fonts["head"], fill=GOLD)
            y += 26
        elif kind == "body":
            d.text((PAD + 8, y), item["text"], font=fonts["body"], fill=item["color"])
            y += 24
        elif kind == "kv":
            x = PAD + 8
            for txt, color in item["parts"]:
                d.text((x, y), txt, font=fonts["body"], fill=color)
                x += d.textlength(txt, font=fonts["body"])
            y += 24
        elif kind == "line":
            d.line([PAD + 8, y + 2, W - PAD, y + 2], fill=BORDER)
            y += 10
        elif kind == "row":
            font = fonts[item["font"]]
            for cx, cell in zip(COLS, item["cells"]):
                if cell:
                    color = GOLD if cell.isdigit() and cx == COLS[1] else item["color"]
                    d.text((cx, y), cell, font=font, fill=color)
            y += 24

    return img


def build(out: Path, dump_frames: bool = False) -> None:
    fonts = load_fonts()
    frames: list[Image.Image] = []
    durations: list[int] = []

    # 阶段 1：打字。
    # 不画"打完整句 + 光标"这一帧，它与阶段 2 的首帧完全相同，
    # 会被 PIL 合并掉，导致统计帧数比实际多 1。
    for i in range(0, len(QUERY), TYPE_STEP):
        frames.append(render(fonts, QUERY[:i], 0, cursor=True))
        durations.append(90)

    # 阶段 2：输入完成，光标闪两下
    for blink in (True, False, True):
        frames.append(render(fonts, QUERY, 0, cursor=blink))
        durations.append(260)

    # 阶段 3：逐条输出。
    # gap 只增加纵向间距、不绘制内容，单独成帧会与前一帧完全相同，
    # PIL 保存时本就会把它合并掉，所以直接跳过，避免统计帧数与实际不符。
    for n in range(1, len(ITEMS) + 1):
        if ITEMS[n - 1]["kind"] == "gap":
            continue
        frames.append(render(fonts, QUERY, n, cursor=False))
        durations.append(80 if ITEMS[n - 1]["kind"] == "line" else 230)

    # 阶段 4：结果停留
    durations[-1] = 3000

    out.parent.mkdir(parents=True, exist_ok=True)
    # 不传 loop 参数，GIF 只播一次后停在最后一帧。
    # 循环播放会在读者看完表格前就重新开始，反而看不完。
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        optimize=True,
    )

    if dump_frames:
        fdir = out.parent / "frames"
        fdir.mkdir(exist_ok=True)
        for i, fr in enumerate(frames):
            fr.save(fdir / f"frame_{i:02d}.png")

    size_kb = out.stat().st_size / 1024
    print(f"已生成 {out}")
    print(f"尺寸 {W}x{H}，{len(frames)} 帧，总时长 {sum(durations) / 1000:.1f}s，{size_kb:.0f} KB")
    if size_kb > 2048:
        print("[提示] 超过 2MB，GitHub 上加载会偏慢，建议减少帧数或缩小画布")


def main() -> None:
    p = argparse.ArgumentParser(description="生成演示 GIF")
    p.add_argument("--out", default="assets/demo.gif", help="输出路径")
    p.add_argument("--frames", action="store_true", help="同时导出每帧 PNG")
    args = p.parse_args()
    build(Path(args.out), args.frames)


if __name__ == "__main__":
    main()
