# Stock Radar · 每日股票信息雷达

每天自动汇总四类信息，生成 Markdown / HTML / JSON 报告，可推送到 Slack、Telegram、邮箱或自定义 Webhook。
**全部来自官方或公开披露源，默认零 API key、零付费。**

| 板块 | 内容 | 数据来源 | 时效 |
| --- | --- | --- | --- |
| 🏛️ 国会议员交易 | 议员及配偶/子女的每一笔买卖：标的、代码、方向、金额区间、交易日、披露延迟 | **众议院书记官办公室**年度索引 + PTR 原始 PDF；**参议院 eFD** 电子报告 | 法律允许最多延迟 45 天 |
| 👔 公司高管买卖 | 董事、高管、10% 股东的增减持（Form 4） | SEC EDGAR | 交易后 2 个工作日内 |
| 🏦 基金持仓变化 | 机构季度持仓的新建仓/加仓/减仓/清仓（13F），大额持股举牌（13D/13G） | SEC EDGAR | 13F 季度；13D/G 事件驱动 |
| 📰 重要新闻 | 市场新闻 + 关注个股新闻 + 关注公司的 8-K 重大事件公告 | CNBC / Yahoo Finance / MarketWatch / SEC / Fed 等 RSS | 实时 |

国会板块**不依赖任何第三方镜像**——原先常用的 house/senate stock watcher 公开数据集已经关闭访问，
这里直接读两院官方源，包括把众议院的 PTR PDF 解析成结构化交易记录。

## ⚠️ 先看这一条：在哪台机器上跑

**SEC EDGAR 会按 IP 限流。** 云端共享出口 IP（GitHub Actions、大部分云主机）长期处于超限状态，
返回的 403 页面原文就是 `SEC.gov | Request Rate Threshold Exceeded`。这跟 User-Agent 无关，换 UA 没用。

| 运行环境 | 国会 | 新闻 | 高管 Form 4 | 基金 13F/13D | 建议 |
| --- | :-: | :-: | :-: | :-: | --- |
| **自己的电脑/服务器**（家庭或公司网络） | ✅ | ✅ | ✅ | ✅ | **推荐**，四个板块都能跑 |
| GitHub Actions | ✅ | ✅ | ❌ | ❌ | 只想要国会+新闻时够用 |

所以默认建议本机跑（下面有一键安装脚本）。真被 SEC 挡住时，报告的"数据源状态"区块会直接写明原因和处理办法，
不会让你误以为"今天没消息"。

## 60 秒上手

```bash
cd stock-radar
pip install -r requirements.txt

python -m stock_radar init          # 生成 config.yaml
# 编辑 config.yaml：填 sec.user_agent（SEC 要求带联系邮箱），改 watchlist.tickers
python -m stock_radar run           # 跑一次，报告写到 out/
```

打开 `out/latest.html` 是当天报告，`out/latest.md` 适合贴笔记，`out/latest.json` 给下游程序用。

### 每天自动跑（本机，推荐）

```bash
bash install-schedule.sh            # 只打印将要做什么，不改任何东西
bash install-schedule.sh --apply    # 确认后再执行
```

macOS 装 launchd 任务，Linux 写 crontab，默认工作日 7:30 本地时间。
改时间：`HOUR=6 MINUTE=45 bash install-schedule.sh --apply`。卸载方法脚本会打印。

### 每天自动跑（GitHub Actions）

仓库已带 `.github/workflows/stock-radar.yml`，工作日盘前触发，报告出现在运行的 Summary 页和 Artifact 里。
1. 把改好的 `config.yaml` 提交进仓库——**里面不含任何密钥**（只写环境变量的名字），
   不提交的话 Actions 会退回用 `config.example.yaml` 的默认关注列表；
2. 在 **Settings → Secrets and variables → Actions** 加 `SEC_USER_AGENT`（想推送再加对应 secret）。
跨天去重的状态库通过 `actions/cache` 保存。注意上面的表：这条路上 SEC 那两个板块大概率拿不到数据。

## 配置要点

```yaml
sec:
  user_agent: "Your Name your-email@example.com"   # SEC 要求带能联系到你的邮箱

watchlist:
  tickers: [AAPL, NVDA, TSLA]   # 高管买卖 / 8-K / 个股新闻默认只跟这些
  people: []                    # 想重点盯的议员全名，命中会加权
  funds:                        # 要跟踪 13F 的基金
    - {name: "Berkshire Hathaway", cik: "0001067983"}
```

CIK 别手抄，用内置命令查：

```bash
python -m stock_radar find-fund berkshire
#   - {name: "BERKSHIRE HATHAWAY INC", cik: "0001067983"}
```

第一次跑会把该基金最大持仓当作基线，之后每季新 13F 一到就自动 diff 出变动。

## 推送渠道

配置里只写**环境变量的名字**，真正的密钥放环境变量 / GitHub Secrets，不进 git。

| 渠道 | 打开方式 | 需要的环境变量 |
| --- | --- | --- |
| Slack | `notify.slack.enabled: true` | `SLACK_WEBHOOK_URL` |
| Telegram | `notify.telegram.enabled: true` | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| 邮件 | `notify.email.enabled: true` + 填 `from_addr` / `to_addrs` | `SMTP_HOST`、`SMTP_USER`、`SMTP_PASSWORD` |
| 自定义 Webhook | `notify.webhook.enabled: true` | `STOCK_RADAR_WEBHOOK`（收到完整 JSON） |

## 常用命令

```bash
python -m stock_radar run --only news,congress   # 只跑部分板块
python -m stock_radar run --date 2026-09-03      # 回补某一天
python -m stock_radar run --no-dedup --dry-run   # 调试：不去重、不写文件
python -m stock_radar -v run                     # 看每个数据源到底抓到了什么

python -m stock_radar probe                      # 体检：逐个打真实数据源，检查格式假设还成不成立
python -m stock_radar diagnose                   # 深度诊断：SEC 准入矩阵、国会源存活、PDF 正文
python -m stock_radar find-fund "pershing square"
```

**数据源出问题时先跑 `probe`。** 它会分清三种情况：网络失败、HTTP 成功但解析出 0 条（我们的 bug）、
以及格式漂移（比如 13F 金额单位判断落在不合理区间）。

## 调参建议

- **信息太多**：`sources.news.watchlist_only: true`、`sources.congress.watchlist_only: true`，
  再调高 `sources.insiders.min_value_usd`。
- **只关心真金白银的买入**：`sources.insiders.codes: [P]`。默认 `[P, S]` 已排掉授予（A）、
  行权（M）、缴税扣股（F）——高管"卖出"里很大一部分其实是预设计划和税务操作。
- **国会想连国债一起看**：`sources.congress.stocks_only: false`（默认只报股票/期权）。
- **第一次跑国会板块慢**：要下载并解析 45 天内的所有 PTR PDF。解析结果会缓存进状态库，
  之后每天只处理新增的。`max_filings` 控制单次上限。
- **想看全市场高管交易**：`sources.insiders.scope: all`，会从几十个请求涨到几百个。

## 报告长这样

```
## 🏛️ 国会议员交易 (12)
- **Jane Doe (House) 买入 NVDA [配偶]** `2026-08-14`
  - $250,001 - $500,000 · 交易日 2026-08-14 · 延迟披露 19 天

## 👔 公司高管买卖 (Form 4) (3)
- **AAPL · COOK TIMOTHY D 卖出 511,000 股 ($115.70M)** `2026-09-03`
  - Director, Chief Executive Officer · 成交价 $226.42 · 交易后持股 3,280,000

## 🏦 基金持仓变化 (13F / 13D / 13G) (4)
- **Berkshire Hathaway 新建仓 CHUBB LIMITED** `2026-09-01`
  - 27,033,784 股 · $6.72B
```

报告末尾固定有**数据源状态**区块。某个源挂了会显示 ❌ 和具体原因——
"今天没消息"和"今天抓取失败"不会长得一样。

## 设计说明

- **跨天去重**：每条内容有稳定 key，存在 `.stock-radar/state.db`。国会披露会在滚动窗口里
  留存好几周，没有去重同一笔交易会连推 45 天。
- **PTR 解析结果也缓存**：一份 PTR 的内容永远不变，没必要每天重新下载解析。
- **覆盖缺口要报出来**：扫描件 PTR 抽不出表格是整个开源社区的普遍未解问题，别人的做法多是静默丢弃。
  这里会给出 PDF 链接，并在数据源状态里写明「N/M 份是扫描件」，让你知道这期少了什么。
- **Form 4 的两个歧义**：`4/A` 是对既有申报的**修正**而非新增交易，报告里会标【修正申报】并提示别与原件重复计数；
  通过信托等实体的间接持有会标 [间接持有] 并附持有形式——同样的金额，含义不同。
- **ticker 可能为空**：自由文本资产名解析成代码，即便是商业厂商也只做到约 68%。
  解析不出代码的行不会被丢掉，保留原始资产名；`stocks_only` 判断同时看代码和资产类型，不只看代码。
- **失败必须看得见**：单个源失败只让那个板块变 ❌；连采集器内部吞掉的子失败（某个 RSS 挂了、
  某只基金 403）也会折进状态区块，不会显示成 ✅ 0 条。
- **13F 金额单位**：2023 年规则修改前 `<value>` 报千美元、之后报美元，旧格式仍会出现在修正案里。
  代码按"每股隐含价格"推断单位，不按年份硬编码——这里搞错就是 1000 倍误差。
- **PTR PDF 解析**：抽出来的文本会在行中间换行，小型大写标题会变成 NUL 字符。解析器保留行结构，
  用"交易代码 + 两个日期 + 金额区间"这个永远完整的形状定位每一行，资产名取它正上方的行——
  这才能把本行资产和上一行的尾注分开。
- **SEC 限速**：全局限速器默认 5 req/s（SEC 上限 10），并强制带 User-Agent；配置里留着模板占位值会被检测出来。
- **依赖只有三个**：`requests`、`PyYAML`、`pypdf`。RSS/Atom 解析和 HTML 表格解析都是标准库写的。

## 已验证到什么程度

`probe` 和 `diagnose` 会在 GitHub Actions 上跑（`.github/workflows/stock-radar-probe.yml`，
每周一定时 + 每次改动代码时），打的是真实端点。截至最近一次运行：

- ✅ 7/7 新闻 RSS 源可用，条目、时间戳、摘要都正确解析（单次 40 条）
- ✅ 众议院：年度索引 → PTR PDF → 解析出真实交易；参议院：同意条款 + CSRF + 搜索 + 电子报告表格全通
- ✅ 一次真实运行拿到 **448 条**两院交易，例如
  `David J. Taylor (House) 买入 GOOGL $1,001-$15,000 · 延迟披露 20 天`、
  `Sheldon Whitehouse (Senate) 部分卖出 NVDA $15,001-$50,000 · 延迟披露 20 天`
- ❌ SEC EDGAR 在 GitHub 的 IP 上被限流（见上文），Form 4 / 13F 需要在本机验证

> 第一次跑国会板块会一次性输出 45 天的存量（几百条），之后每天只报新增。

`python -m pytest -q` 有 81 个测试，全部离线跑，覆盖真实格式样本
（Form 4 XML、13F 信息表、EDGAR master.idx、RSS/Atom、众议院 PTR PDF、参议院 eFD 表格、SEC 的两种 403 页面）。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试用 `tests/conftest.py` 的假 HTTP 层按 URL 匹配返回 `tests/fixtures/` 里的样本，
连众议院 ZIP 索引和 PTR PDF 都是测试里现造的真文件，会走一遍真实的 pypdf 抽取。
改解析逻辑时先跑它，再看 `probe` 的线上结果。

## 合规注意

- **数据来源决定你能怎么用。** 从 disclosures-clerk.house.gov / efdsearch.senate.gov / SEC EDGAR
  直接取得的是政府一手公开文件，事实本身不受版权保护，自用和公开转述都没问题——这也是默认配置只走官方源的原因。
- **商业 API 数据不能进任何公开渠道。** 可选的 `quiver` provider 走的是 Quiver Quantitative 的付费 API，
  其服务条款把数据授权限定为「个人非商业用途」，并明确禁止以任何形式再分发给第三方（**包括非商业场景**），
  同时禁止用任何自动化手段抓取其网站。所以：开了 `quiver` 就只能自己看，
  别把报告发进公开的 Telegram 频道 / GitHub Pages / newsletter。抓它的网页当免费替代属于违约，不要做。
- **别把第三方镜像当生产数据源。** 社区里被广泛引用的 house/senate stock watcher 派生数据集实际已冻结数年
  （最新数据停在 2021 年），README 却仍写着持续更新。这类源可以拿来一次性回填历史，不该进每日调度。

## 免责声明

本工具只做公开披露数据的自动汇总，**不构成投资建议**。披露本身存在法定延迟（国会最多 45 天，
13F 为季度且滞后 45 天），重要决策请以 SEC EDGAR、众议院/参议院官方披露页面的原始文件为准。
