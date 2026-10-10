# MCP 集成说明

## 使用的 MCP Server

| 项目 | 值 |
|---|---|
| 名称 | 麦当劳 MCP Server（McDonald's China MCP Server） |
| 提供方 | 麦当劳中国 |
| 接入地址 | `https://mcp.mcd.cn` |
| 官方文档 | https://github.com/M-China/mcd-mcp-server |
| Token 申请 | https://open.mcd.cn/mcp |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | MCP 2025-06-18 及之前 |
| 限流 | 600 次 / 分钟 / Token，超限返回 429 |

## 调用的 Tool

本 Skill 实际调用 7 个 Tool，其中 3 个为必调，4 个按场景触发。

### 必调（每次执行）

| Tool | 用途 | 为什么必须 |
|---|---|---|
| `now-time-info` | 获取服务端当前时间 | 商品下架时间需与服务端时钟对齐，本地时间会导致误判 |
| `query-my-account` | 读取积分账户 | 取可用积分、本月/下月将过期积分、历史已过期积分 |
| `mall-points-products` | 拉取积分兑换商品清单 | 方案的候选集来源 |

### 按需调用

| Tool | 触发条件 |
|---|---|
| `mall-product-detail` | 用户关注某个商品时，取 `skuList[].skuId` 与 `spuCategory` |
| `mall-create-order` | 用户确认兑换后。虚拟券与实物商品均走此工具，由入参 `spuCategory` 区分 |
| `delivery-query-addresses` | 兑换实物（`spuCategory="2"`）需要收货地址时 |

## 调用流程

```
now-time-info ──┐
                ├─→ 数据清洗 ─→ 排序 ─→ 输出方案 ─→ 用户确认 ─→ mall-create-order
query-my-account┤                                    │
                │                                    └─ mall-product-detail
mall-points-    ┘                                       （取 skuId + spuCategory）
products
```

前三个调用互相独立，可并行发起。清洗与排序全部在本地完成，不产生额外网络请求。

## 业务价值

麦当劳积分按月过期。过期额度和可兑商品都可以查到，但需要用户自己留意时间、自己比对哪些商品兑得划算，而大多数人不会每月专门做一次。

这个 Skill 把三件原本需要手动完成的事自动化：

第一，把"还有多少分即将作废"这个数字直接摆出来。`query-my-account` 提供了 `currentMouthExpirePoint` 和 `nextMouthExpirePoint`，但用户不会主动去查。

第二，剔除已过兑换截止的商品。清单中存在 `status=2` 但 `downTime` 已早于当前时间的记录，需按 `downTime` 二次过滤，否则结果里会混入兑不了的商品。

第三，同权益去重取优。清单中存在名称与权益完全相同、但所需积分不同的商品。按 `spuName` 分组取 `point` 最小值，可以避免用户为同一份权益多支付积分。

## 实现中处理的数据细节

以下几点来自实际调用返回的数据，文档中未说明：

可兑换状态需结合 `downTime` 与 `upTime` 判定，不能仅依据 `status`。一次实测返回 50 件商品 `status` 均为 `2`，其中 19 件的 `downTime` 已早于服务端当前时间。判定基准取服务端时间。

`query-my-account` 的过期积分字段名为 `currentMouthExpirePoint` / `nextMouthExpirePoint`，读取时按此名称取值。

`shopId` 决定兑换链路。`shopId=2` 为券和实物，走 `mall-create-order` 系列；`shopId=5` 为派对、体验营、品鉴会一类活动，`point` 为 0 而 `price` 为现金金额，需走门店场次预约流程，不能用 `mall-create-order`，也不应计入积分兑换方案。

所有积分字段均为字符串类型且可能带小数，例如 `point` 返回 `"800"`、`"50"`，`currentMouthExpirePoint` 返回 `"500.5"`，`expiredPoint` 返回 `"4150.25"`。排序与比较前必须显式转为数字，否则字符串比较下 `"100" < "50"` 成立，排序结果会静默错误且不抛异常。

兑换下单使用 `skuId`，不是 `mall-points-products` 返回的 `spuId`。`skuId` 与必填的 `spuCategory` 都需要通过 `mall-product-detail` 获取，列表接口不返回 `spuCategory`，也不能从 `catName` 推断。

兑换统一走 `mall-create-order`，通过入参 `spuCategory`（`"1"` 虚拟商品 / `"2"` 实体商品）区分类型，实物额外传 `addressId`。实现时以 MCP Server 实际暴露的工具列表为准。

`mall-points-products` 未提供分页字段。返回条数接近整数上限时无法判断结果是否被截断，"固定 3 次调用"的假设在商品数量增长后可能不再成立。

## 安全设计

`mall-create-order` 会真实扣减积分，操作不可撤销。本 Skill 的约束：

- 未获得用户明确确认，不调用任何写操作 Tool
- 调用前回显商品名、`skuId`、`spuCategory`、消耗积分、数量
- 一次只兑换一件商品，不批量下单
- MCP Token 只从环境变量读取，不出现在仓库任何文件中

本项目的盘点流程只使用只读 Tool，可在不产生任何交易的前提下完整运行。
