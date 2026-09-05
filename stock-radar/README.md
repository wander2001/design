# Stock Radar · 每日股票信息雷达

每天自动汇总四类信息，生成一份 Markdown / HTML / JSON 报告，并可推送到 Slack、Telegram、邮箱或自定义 Webhook：

| 板块 | 内容 | 数据来源 | 时效 |
| --- | --- | --- | --- |
| 🏛️ 国会议员交易 | 参议员 / 众议员及其配偶的股票买卖 | House / Senate Stock Watcher（免费镜像）、Quiver（可选 API key）、众议院官方索引（兜底） | 法律允许延迟最多 45 天披露 |
| 👔 公司高管买卖 | 董事、高管、10% 股东的增减持（Form 4） | SEC EDGAR | 交易后 2 个工作日内 |
| 🏦 基金持仓变化 | 机构季度持仓的新建仓 / 加仓 / 减仓 / 清仓（13F），以及大额持股举牌（13D/13G） | SEC EDGAR | 13F 季度披露（季末后 45 天）；13D/13G 事件驱动 |
| 📰 重要新闻 | 市场新闻 RSS + 关注个股的 8-K 重大事件公告 | CNBC / Yahoo Finance / MarketWatch / SEC / Fed 等 RSS | 实时 |

全部数据来自公开披露，**不需要付费数据源，默认零 API key**（Quiver 是可选增强）。

## 60 秒上手

```bash
cd stock-radar
pip install -r requirements.txt

python -m stock_radar init            # 生成 config.yaml
# 编辑 config.yaml：填 sec.user_agent（SEC 强制要求带联系邮箱），改 watchlist
python -m stock_radar run             # 跑一次，报告写到 out/
```

打开 `out/latest.html` 就是当天的报告，`out/latest.md` 适合贴到笔记里，`out/latest.json` 给下游程序用。

### 必须先改的两处配置

```yaml
sec:
  # SEC 要求自动化请求带可联系到你的 UA，否则会被限流甚至封 IP
  user_agent: "Your Name your-email@example.com"

watchlist:
  tickers: [AAPL, NVDA, TSLA]     # 高管买卖 / 8-K / 个股新闻默认只跟这些
```

### 跟踪某只基金的持仓

CIK 别手抄，用内置命令查：

```bash
python -m stock_radar find-fund berkshire
#   - {name: "BERKSHIRE HATHAWAY INC", cik: "0001067983"}
```

把输出粘进 `config.yaml` 的 `watchlist.funds` 即可。第一次跑会把该基金的最大持仓当作基线，
之后每个季度新的 13F 一到就自动 diff 出变动。

## 每天自动跑

### 方式一：GitHub Actions（推荐，零成本）

仓库里已经带了 `.github/workflows/stock-radar.yml`，每个工作日美东盘前触发。你只要：

1. 在仓库 **Settings → Secrets and variables → Actions** 加 `SEC_USER_AGENT`；
2. 想要推送就再加对应的 secret（见下表）；
3. 报告会出现在每次运行的 **Summary** 页和 Artifact 里。

跨天去重用的 sqlite 状态库通过 `actions/cache` 保存，所以同一条披露不会天天重复推给你。

### 方式二：本机 cron

```cron
30 7 * * 1-5 cd /path/to/stock-radar && /usr/bin/python3 -m stock_radar run >> radar.log 2>&1
```

## 推送渠道

配置文件里只写**环境变量的名字**，真正的密钥放环境变量 / GitHub Secrets，不进 git。

| 渠道 | 打开方式 | 需要的环境变量 |
| --- | --- | --- |
| Slack | `notify.slack.enabled: true` | `SLACK_WEBHOOK_URL` |
| Telegram | `notify.telegram.enabled: true` | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| 邮件 | `notify.email.enabled: true` + 填 `from_addr` / `to_addrs` | `SMTP_HOST`、`SMTP_USER`、`SMTP_PASSWORD` |
| 自定义 Webhook | `notify.webhook.enabled: true` | `STOCK_RADAR_WEBHOOK`（收到完整 JSON） |

## 常用命令

```bash
python -m stock_radar run --only news,insiders   # 只跑部分板块
python -m stock_radar run --date 2026-09-03      # 回补某一天
python -m stock_radar run --no-dedup --dry-run   # 调试：不去重、不写文件
python -m stock_radar run -v                     # 看每个数据源到底抓到了什么
python -m stock_radar find-fund "pershing square"
```

## 调参建议

- **信息太多**：把 `sources.news.watchlist_only` 和 `sources.congress.watchlist_only` 设成 `true`，
  再调高 `sources.insiders.min_value_usd`。
- **只关心真金白银的买入**：`sources.insiders.codes: [P]`。默认的 `[P, S]` 已经排掉了
  授予（A）、行权（M）、缴税扣股（F）这些噪音——高管"卖出"里很大一部分其实是预设计划和税务操作。
- **想看全市场高管交易**：`sources.insiders.scope: all`。注意这会让每次运行从几十个请求涨到几百个，
  受 `max_filings` 限制，跑一次大概几分钟。
- **国会数据一条都没有**：免费镜像偶尔停更。把 `providers` 改成
  `[stockwatcher, house_clerk]`（默认已经是），至少能拿到"某议员提交了 PTR"和 PDF 链接；
  有 Quiver key 的话 `[quiver, stockwatcher]` 最稳。

## 报告长这样

```
## 🏛️ 国会议员交易 (3)
- **Jane Doe (House) 买入 NVDA [joint]** `2026-08-14`
  - $250,001 - $500,000 · 交易日 2026-08-14 · 延迟披露 19 天

## 👔 公司高管买卖 (Form 4) (2)
- **AAPL · COOK TIMOTHY D 卖出 511,000 股 ($115.70M)** `2026-09-03`
  - Director, Chief Executive Officer · 成交价 $226.42 · 交易后持股 3,280,000

## 🏦 基金持仓变化 (13F / 13D / 13G) (4)
- **Berkshire Hathaway 新建仓 CHUBB LIMITED** `2026-09-01`
  - 27,033,784 股 · $6.72B
```

报告末尾固定有一个**数据源状态**区块。某个源挂了会显示 ❌ 和原因——
这样"今天没消息"和"今天抓取失败"不会长得一样。

## 设计说明

- **跨天去重**：每条内容有稳定的 key，存在 `.stock-radar/state.db`。国会披露数据会在
  滚动窗口里留存好几周，没有去重的话同一笔交易会连着推 45 天。
- **单个数据源失败不影响其他板块**：collector 抛异常只会让那一个板块变成 ❌，其余照常出。
- **13F 金额单位**：2023 年规则修改前 `<value>` 报的是千美元、之后是美元，而且旧格式仍会出现在
  修正案里。代码按"每股隐含价格"推断单位，不硬编码年份。
- **SEC 限速**：全局限速器默认 5 req/s（SEC 上限 10），并强制带 User-Agent。
- **依赖只有两个**：`requests` 和 `PyYAML`。RSS/Atom 解析是自己写的，少一个依赖就少一个
  无人值守时崩掉的理由。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 57 个测试，全部离线跑，不碰网络
```

测试用 `tests/conftest.py` 里的假 HTTP 层，按 URL 匹配返回 `tests/fixtures/` 里的真实格式样本
（Form 4 XML、13F 信息表、EDGAR master.idx、RSS/Atom、国会交易 JSON）。改解析逻辑时先跑它。

## 免责声明

本工具只做公开披露数据的自动汇总，**不构成投资建议**。免费镜像数据源可能滞后或缺失，
重要决策请以 SEC EDGAR、众议院/参议院官方披露页面的原始文件为准。
