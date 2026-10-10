"""麦当劳积分兑换清单的清洗与排序。

对应 SKILL.md 第 4、5 步。逻辑独立于 AI，可单独运行与测试。

处理 mall-points-products 返回数据时的三个要点：
1. 可兑换状态按服务端时间与 downTime / upTime 判定，不能仅依据 status
2. 同名商品可能存在不同积分价，需去重取最低
3. shopId 决定兑换链路，shopId=5 的活动不消耗积分，需排除出积分方案
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

import re

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SHOP_COUPON_AND_GOODS = 2  # 餐品券、实物周边，走 mall-create-order
SHOP_PARTY_ACTIVITY = 5  # 派对/体验营/品鉴会，需门店场次预约，不消耗积分

# 「略微多花」档的上限 = 过期额度 + min(过期额度 * RATIO, ABS_CAP)
NEAR_RATIO = 0.5
NEAR_ABS_CAP = 200.0

MAX_TOTAL_ITEMS = 10  # 清单总条数上限，按档顺序填充，填满即停

FACE_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")


def parse_face_value(spu_name: str) -> float | None:
    """从商品名解析券面金额，如「21.9元巨无霸可乐组合」→ 21.9。

    这是用券后的价格，不是商品原价。接口不提供原价，所以无法计算真实优惠额。
    此处仅把它当作「消费档位」的代理指标：档位越高，通常对应的商品越贵。
    绝不能据此声称为用户省了多少钱。
    """
    m = FACE_VALUE_RE.search(spu_name)
    return float(m.group(1)) if m else None


@dataclass(frozen=True)
class Product:
    spu_id: int
    spu_name: str
    point: float
    price: str
    shop_id: int
    cat_name: str
    status: int
    up_time: datetime | None
    down_time: datetime | None

    @property
    def costs_points(self) -> bool:
        return self.point > 0


@dataclass
class CleanResult:
    redeemable: list[Product]
    expired: list[Product]
    preheating: list[Product]
    party_activities: list[Product]
    zero_point: list[Product]
    deduped_savings: list[tuple[str, float]]
    dropped_count: int  # 同名去重丢弃的条数，用于数据完整性自检
    total_count: int  # 接口返回的原始条数


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, TIME_FORMAT)
    except ValueError:
        return None


def parse_products(raw_items: Iterable[dict]) -> list[Product]:
    """把 mall-points-products 的 data 数组转成 Product 列表。"""
    products: list[Product] = []
    for item in raw_items:
        try:
            point = float(item.get("point") or 0)
        except (TypeError, ValueError):
            point = 0.0
        products.append(
            Product(
                spu_id=int(item.get("spuId") or 0),
                spu_name=str(item.get("spuName") or "").strip(),
                point=point,
                price=str(item.get("price") or "0"),
                shop_id=int(item.get("shopId") or 0),
                cat_name=str(item.get("catName") or ""),
                status=int(item.get("status") or 0),
                up_time=_parse_time(item.get("upTime")),
                down_time=_parse_time(item.get("downTime")),
            )
        )
    return products


def clean(products: Sequence[Product], now: datetime) -> CleanResult:
    """按 SKILL.md 第 4 步执行清洗。

    now 必须来自 now-time-info 的服务端时间，不要传本地时间。
    """
    expired: list[Product] = []
    preheating: list[Product] = []
    party: list[Product] = []
    zero_point: list[Product] = []
    candidates: list[Product] = []

    for p in products:
        # 4.1 以 downTime / upTime 判定，不仅依据 status
        if p.down_time is not None and p.down_time < now:
            expired.append(p)
            continue
        if p.up_time is not None and p.up_time > now:
            preheating.append(p)
            continue
        # 4.3 派对类活动走预约链路，不进积分方案
        if p.shop_id == SHOP_PARTY_ACTIVITY:
            party.append(p)
            continue
        # 4.4 不消耗积分的记录对"花掉积分"无意义
        if not p.costs_points:
            zero_point.append(p)
            continue
        candidates.append(p)

    redeemable, savings = dedupe_by_name(candidates)
    return CleanResult(
        redeemable=redeemable,
        expired=expired,
        preheating=preheating,
        party_activities=party,
        zero_point=zero_point,
        deduped_savings=savings,
        dropped_count=len(candidates) - len(redeemable),
        total_count=len(products),
    )


def dedupe_by_name(
    products: Sequence[Product],
) -> tuple[list[Product], list[tuple[str, float]]]:
    """4.2 同名商品保留积分价最低的一条。

    返回 (去重后列表, [(商品名, 省下的积分), ...])。
    """
    best: dict[str, Product] = {}
    for p in products:
        current = best.get(p.spu_name)
        if current is None or p.point < current.point:
            best[p.spu_name] = p

    savings: list[tuple[str, float]] = []
    for name, keeper in best.items():
        others = [p.point for p in products if p.spu_name == name and p.point > keeper.point]
        if others:
            savings.append((name, max(others) - keeper.point))

    return list(best.values()), sorted(savings, key=lambda x: -x[1])


def rank(
    products: Sequence[Product],
    budget: float,
) -> list[Product]:
    """第 5 步排序：预算内的优先，其次积分低，其次快下架，最后 spuId 兜底。

    spuId 这一级不可省略。实测存在 point 与 downTime 完全相同的商品
    （spuId 17073 与 17074 均为 50 分、同一截止时间），缺少兜底规则时
    排序结果不确定，同一份数据两次运行可能给出不同顺序。
    """
    far_future = datetime.max

    def sort_key(p: Product):
        return (
            0 if p.point <= budget else 1,
            p.point,
            p.down_time or far_future,
            p.spu_id,
        )

    return sorted(products, key=sort_key)


def plan_budget(available_point: float, expire_point: float) -> float:
    """第一档预算：即将过期的额度优先，没有过期额度时退回全部可用积分。"""
    return min(available_point, expire_point) if expire_point > 0 else available_point


def near_limit(expire_point: float) -> float:
    """「略微多花」档的积分上限。"""
    return expire_point + min(expire_point * NEAR_RATIO, NEAR_ABS_CAP)


def build_recommendations(
    products: Sequence[Product],
    available_point: float,
    expire_point: float,
) -> tuple[list[tuple[Product, str]], float]:
    """按用户心智分三档生成推荐，返回 ([(商品, 推荐理由)], 第一档合计积分)。

    第一档「用掉快过期积分」：在过期额度内贪心装箱，是用户的主要目的。
    第二档「略微多花」：单件积分略超过期额度，用户往往愿意多付一点换更好的。
    第三档「券面价值更高」：积分数额更大，但对应的消费档位更高。

    总条数不超过 MAX_TOTAL_ITEMS，按档顺序填充，填满即停。不限制各档的件数，
    因为第一档最贴近用户目的，它多占位是合理的。

    只呈现用户能兑换的商品。被过滤掉的商品不进入结果，用户对全量清单没有概念。
    """
    budget = plan_budget(available_point, expire_point)
    near_cap = near_limit(expire_point) if expire_point > 0 else available_point

    result: list[tuple[Product, str]] = []
    used: set[int] = set()

    def room_left() -> bool:
        return len(result) < MAX_TOTAL_ITEMS

    # 第一档：装满过期额度
    spent = 0.0
    for p in rank(products, budget):
        if spent + p.point <= budget:
            # 即使清单已满也要继续累计 spent，否则「能用掉多少分」会算少
            if room_left():
                result.append((p, "用掉快过期积分"))
                used.add(p.spu_id)
            spent += p.point

    # 第二档：略超过期额度，但在可用积分内
    near = [
        p for p in products
        if p.spu_id not in used and budget < p.point <= near_cap and p.point <= available_point
    ]
    for p in sorted(near, key=lambda p: (p.point, p.spu_id)):
        if not room_left():
            break
        extra = p.point - expire_point
        note = f"多付 {extra:g} 分" if extra > 0 else "略微多花"
        result.append((p, note))
        used.add(p.spu_id)

    # 第三档：券面金额更高的选择。无面值的排最后，避免挤掉有信息的条目
    rest = [p for p in products if p.spu_id not in used and p.point <= available_point]
    rest.sort(key=lambda p: (-(parse_face_value(p.spu_name) or -1), p.point, p.spu_id))
    for p in rest:
        if not room_left():
            break
        face = parse_face_value(p.spu_name)
        note = f"券面 {face:g} 元，档位更高" if face is not None else "积分较高，按需选择"
        result.append((p, note))
        used.add(p.spu_id)

    return result, spent


def format_report(
    result: CleanResult,
    available_point: float,
    expire_point: float,
    expired_point: float = 0.0,
    include_history: bool = False,
) -> str:
    """生成用户可读的报告。

    include_history 默认关闭：历史已过期积分属于无法挽回的损失，主动摆出来
    只会引发损失厌恶情绪，像在指责用户，而不是帮他解决问题。仅在用户明确
    询问「我以前过期了多少」时才传 True。
    """
    recs, _spent = build_recommendations(result.redeemable, available_point, expire_point)

    def render_rows(items: Sequence[tuple[Product, str]]) -> list[str]:
        """只输出名称、积分、兑换截止三列。

        推荐理由不展示。三档的排序顺序本身已经承载了推荐逻辑：
        先是能用掉过期积分的，再是略微多花的，最后是券面档位更高的。
        理由文案（reason）仍在内部保留，用于排序和自检。
        """
        rows = ["| 商品 | 积分 | 兑换截止 |", "|---|---|---|"]
        for p, _reason in items:
            deadline = p.down_time.strftime("%Y-%m-%d") if p.down_time else "未标注"
            rows.append(f"| {p.spu_name} | {p.point:g} | {deadline} |")
        return rows

    lines: list[str] = []

    # 一句话说完账户状况并引导行动，不再单列【过期预警】段。
    # 「本月将过期 YY 分」本身就带紧迫感，后面接行动召唤即可，
    # 不需要再解释缺口多少、怎么补，那些是内部算账口径。
    #
    # 文案说「优惠券」是因为清单里绝大多数是餐品券。若实物商品（周边、鞋袜等）
    # 占比升高，这里的措辞需要跟着调整。
    checkup = f"积分体检：可用 {available_point:g} 分"
    if expire_point > 0:
        checkup += f"，本月将过期 {expire_point:g} 分"
    if include_history:
        checkup += f"，历史已过期 {expired_point:g} 分"
    checkup += "。快看看有哪些优惠券能兑换吧"
    lines.append(checkup)
    lines.append("")

    if recs:
        lines.extend(render_rows(recs))
    else:
        lines.append("当前积分不足以兑换任何在架商品，可以先点餐攒分。")

    # 清单较长时告知用户可能还有更多选择。不暴露总条数和过滤细节，
    # 用户对全量兑换清单没有概念，只需要知道"还能再看看"
    if result.total_count >= 50:
        lines.append("")
        lines.append("可兑换商品较多，以上是按你的积分情况筛选的，想看其他类别可以直接问。")

    # 数据完整性自检：内部质量校验，不作为正常输出展示给用户。
    # 每条返回数据都必须有归属，不平说明清洗逻辑漏了分支。
    ok, accounted = verify_integrity(result)
    if not ok:
        lines.append("")
        lines.append(
            f"[提示] 商品清单核对不一致（已归类 {accounted} / 返回 {result.total_count}），"
            "结果可能不完整"
        )

    return "\n".join(lines)


def verify_integrity(result: CleanResult) -> tuple[bool, int]:
    """内部自检：清洗后各类计数之和必须等于接口返回条数。

    与展示分层无关，只校验清洗阶段有没有漏掉分支。
    """
    accounted = (
        len(result.expired)
        + len(result.preheating)
        + len(result.party_activities)
        + len(result.zero_point)
        + result.dropped_count
        + len(result.redeemable)
    )
    return accounted == result.total_count, accounted


DEMO_PRODUCTS = [
    # 同名不同积分价，应保留 100 分那条
    {"spuName": "10.9元指定小食任选", "spuId": 17076, "point": "100", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:00", "downTime": "2026-12-30 23:59:59"},
    {"spuName": "10.9元指定小食任选", "spuId": 17077, "point": "200", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:03", "downTime": "2026-12-30 23:59:59"},
    # 正常在架低价券
    {"spuName": "6.9元香芋派/菠萝派任选", "spuId": 17074, "point": "50", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:00", "downTime": "2026-12-30 23:59:59"},
    {"spuName": "6.9元可乐麦炫酷", "spuId": 17073, "point": "50", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:03", "downTime": "2026-12-30 23:59:59"},
    # status=2 但已过 downTime，应剔除
    {"spuName": "18.8元2份麦辣鸡翅", "spuId": 6113, "point": "200", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2024-12-31 11:45:03", "downTime": "2026-03-21 23:59:59"},
    {"spuName": "罗技键鼠套装", "spuId": 17694, "point": "6666", "price": "0",
     "shopId": 2, "catName": "玩具", "status": 2,
     "upTime": "2026-08-21 14:00:00", "downTime": "2026-08-21 23:59:59"},
    # shopId=5 活动，point=0、price 为现金价，不进积分方案
    {"spuName": "麦当劳亲子读书会", "spuId": 1830, "point": "0", "price": "45",
     "shopId": 5, "catName": "读书会", "status": 2,
     "upTime": "2023-03-29 00:00:00", "downTime": "2026-12-30 23:59:59"},
    # 略超过期额度，落入「多付一点」档
    {"spuName": "16.9元麦辣可乐组合", "spuId": 17079, "point": "500", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:03", "downTime": "2026-12-30 23:59:59"},
    {"spuName": "17.9元板烧可乐组合", "spuId": 17080, "point": "500", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:00", "downTime": "2026-12-30 23:59:59"},
    # 积分更高但券面档位更高，落入第三档
    {"spuName": "21.9元巨无霸可乐组合", "spuId": 17081, "point": "800", "price": "0",
     "shopId": 2, "catName": "到店专用", "status": 2,
     "upTime": "2026-06-25 00:00:03", "downTime": "2026-12-30 23:59:59"},
]


def _run_demo() -> None:
    now = datetime(2026, 9, 20, 17, 31, 20)
    products = parse_products(DEMO_PRODUCTS)
    result = clean(products, now)

    # 以下积分数值为构造的演示数据，不含任何真实用户账户信息。
    # 过期额度取 400 是为了让三档推荐都有内容：
    # 400 内装下 50+50+100，500 分落入略超档（上限 600），800 分落入券面档。
    demo_available, demo_expiring, demo_expired = 3280.0, 400.0, 4150.0

    print(f"服务端时间：{now.strftime(TIME_FORMAT)}")
    print(f"输入 {len(products)} 件，可兑换 {len(result.redeemable)} 件\n")
    print(format_report(result, demo_available, demo_expiring))

    # 断言核心规则，改动逻辑后可直接跑本文件验证
    names = {p.spu_name for p in result.redeemable}
    assert "18.8元2份麦辣鸡翅" not in names, "已过 downTime 的商品未被剔除"
    assert "罗技键鼠套装" not in names, "已过 downTime 的商品未被剔除"
    assert "麦当劳亲子读书会" not in names, "shopId=5 活动未被排除"
    kept = next(p for p in result.redeemable if p.spu_name == "10.9元指定小食任选")
    assert kept.point == 100, "同名商品未取最低积分价"
    assert kept.spu_id == 17076, "同名去重保留了错误的 spuId"
    recs, spent = build_recommendations(result.redeemable, demo_available, demo_expiring)
    first_tier = [p for p, r in recs if r == "用掉快过期积分"]
    assert spent <= demo_expiring, "第一档超出本月过期额度"
    assert all(p.point <= demo_expiring for p in first_tier), "第一档含超额商品"
    assert len({p.spu_id for p, _ in recs}) == len(recs), "推荐列表出现重复商品"
    assert all(p.point <= demo_available for p, _ in recs), "推荐了积分不够的商品"

    # 分层顺序：第一档必须排在其他档之前，用户先看到能用掉过期分的选项
    tiers = [r for _, r in recs]
    last_first = max(i for i, t in enumerate(tiers) if t == "用掉快过期积分")
    assert all(t == "用掉快过期积分" for t in tiers[: last_first + 1]), "分层顺序被打乱"

    # 数据完整性：内部自检，与展示分层无关
    ok, accounted = verify_integrity(result)
    assert ok, f"数据完整性自检不平：{accounted} != {result.total_count}"

    # 券面金额解析
    assert parse_face_value("21.9元巨无霸可乐组合") == 21.9
    assert parse_face_value("6.9元香芋派/菠萝派任选") == 6.9
    assert parse_face_value("罗技键鼠套装") is None, "无金额的商品名不应解析出面值"

    # 略微多花档的上限
    assert near_limit(500.0) == 700.0, "略超档上限计算错误（500 + min(250, 200)）"
    assert near_limit(100.0) == 150.0, "小额时应按比例而非绝对上限"

    # 「多付一点」档：demo 样例在 500~700 区间没有商品，单独构造用例覆盖
    near_case = parse_products([{
        "spuName": "28.9元板烧四件套", "spuId": 16052, "point": "600", "price": "0",
        "shopId": 2, "catName": "到店专用", "status": 2,
        "upTime": "2026-06-25 00:00:00", "downTime": "2026-12-30 23:59:59",
    }])
    near_recs, near_spent = build_recommendations(near_case, demo_available, demo_expiring)
    assert near_spent == 0, "600 分超出 400 分额度，不应计入第一档"
    assert near_recs[0][1] == "多付 200 分", f"略超档理由错误：{near_recs[0][1]}"

    # 清单条数上限：构造 12 件在架商品，应只保留 10 件
    many = parse_products([
        {"spuName": f"{i}.9元测试券", "spuId": 90000 + i, "point": str(50 + i * 10),
         "price": "0", "shopId": 2, "catName": "到店专用", "status": 2,
         "upTime": "2026-06-25 00:00:00", "downTime": "2026-12-30 23:59:59"}
        for i in range(12)
    ])
    many_recs, _ = build_recommendations(many, 99999.0, 400.0)
    assert len(many_recs) == MAX_TOTAL_ITEMS, (
        f"清单应截断到 {MAX_TOTAL_ITEMS} 件，实际 {len(many_recs)} 件"
    )

    # 三档在 demo 数据上都应有内容，否则演示覆盖不全
    assert any(r == "用掉快过期积分" for _, r in recs), "第一档为空"
    assert any(r.startswith("多付") for _, r in recs), "第二档为空，演示无法覆盖该分支"
    assert any(r.startswith("券面") for _, r in recs), "第三档为空"

    # 运行时输出不得包含内部术语、过滤统计和内部自检口径
    report = format_report(result, demo_available, demo_expiring)
    for banned in ("已剔除", "status", "shopId", "预热", "补充清单", "主推荐", "清洗",
                   "覆盖过期额度", "第一档", "过期预警", "推荐理由", "仍将剩余", "还差"):
        assert banned not in report, f"用户输出里出现了内部术语或过滤统计：{banned}"

    # 无过期积分时不提"本月将过期"，但行动召唤仍要在
    zero_expire_report = format_report(result, demo_available, 0.0)
    assert "本月将过期" not in zero_expire_report, "没有过期积分却提了过期额度"
    assert "快看看有哪些优惠券能兑换吧" in zero_expire_report, "缺少行动召唤"

    # 输出格式：一句体检 + 三列清单
    assert "积分体检：可用 3280 分，本月将过期 400 分。" in report, "体检句格式不符"
    assert "| 商品 | 积分 | 兑换截止 |" in report, "清单不是三列"
    assert "推荐理由" not in report, "清单仍带推荐理由列"
    header_cols = [l for l in report.split("\n") if l.startswith("| 商品")][0].count("|")
    assert header_cols == 4, f"表头列数应为 3（4 个竖线），实际 {header_cols}"

    # 历史已过期积分默认不展示，避免引发损失厌恶情绪
    default_report = format_report(result, demo_available, demo_expiring, demo_expired)
    assert "历史已过期" not in default_report, "默认输出暴露了历史损失"
    assert "4150" not in default_report, "默认输出出现了历史损失数值"
    # 但用户主动询问时要能给出
    asked = format_report(
        result, demo_available, demo_expiring, demo_expired, include_history=True
    )
    assert "历史已过期" in asked, "用户主动询问时应能提供历史损失"
    # 排序必须可复现：同 point 同 downTime 时由 spuId 兜底
    tied = rank(result.redeemable, demo_expiring)
    assert tied == rank(result.redeemable, demo_expiring), "排序结果不稳定"
    # 字符串比较陷阱：point 若不转数字，"100" < "50" 会成立
    assert Product(
        spu_id=1, spu_name="x", point=100.0, price="0", shop_id=2, cat_name="",
        status=2, up_time=None, down_time=None,
    ).point > 50.0, "point 未按数字比较"
    print("\n[OK] 全部断言通过")


def main() -> None:
    parser = argparse.ArgumentParser(description="麦当劳积分兑换清单清洗与排序")
    parser.add_argument("--demo", action="store_true", help="用内置样例数据运行并自检")
    parser.add_argument("--input", help="mall-points-products 原始响应的 JSON 文件路径")
    parser.add_argument("--available", type=float, default=0.0, help="可用积分")
    parser.add_argument("--expiring", type=float, default=0.0, help="本月将过期积分")
    parser.add_argument("--expired", type=float, default=0.0, help="历史已过期积分")
    parser.add_argument("--show-history", action="store_true",
                        help="展示历史已过期积分。默认不展示，避免引发损失厌恶情绪")
    parser.add_argument("--now", help="服务端时间 yyyy-MM-dd HH:mm:ss，来自 now-time-info")
    args = parser.parse_args()

    if args.demo or not args.input:
        _run_demo()
        return

    with open(args.input, encoding="utf-8") as f:
        payload = json.load(f)
    raw_items = payload.get("data", payload) if isinstance(payload, dict) else payload

    now = _parse_time(args.now) or datetime.now()
    result = clean(parse_products(raw_items), now)
    print(format_report(result, args.available, args.expiring, args.expired,
                        include_history=args.show_history))


if __name__ == "__main__":
    main()
