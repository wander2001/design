"""Section titles and chrome, per language."""

SECTIONS = ["congress", "insider", "fund", "news"]

LABELS = {
    "zh": {
        "title": "每日股票雷达",
        "generated": "生成时间",
        "congress": "🏛️ 国会议员交易",
        "insider": "👔 公司高管买卖 (Form 4)",
        "fund": "🏦 基金持仓变化 (13F / 13D / 13G)",
        "news": "📰 重要新闻",
        "empty": "今日无新增。",
        "sources": "数据源状态",
        "ok": "正常",
        "failed": "失败",
        "items": "条",
        "nothing": "所有板块今日均无新增内容。",
        "disclaimer": "本报告由公开披露数据自动汇总，仅供信息参考，不构成投资建议。",
    },
    "en": {
        "title": "Daily Stock Radar",
        "generated": "Generated",
        "congress": "🏛️ Congressional Trades",
        "insider": "👔 Insider Transactions (Form 4)",
        "fund": "🏦 Fund Moves (13F / 13D / 13G)",
        "news": "📰 Top News",
        "empty": "Nothing new today.",
        "sources": "Source status",
        "ok": "ok",
        "failed": "failed",
        "items": "items",
        "nothing": "No new items in any section today.",
        "disclaimer": "Compiled automatically from public disclosures. Information only, not investment advice.",
    },
}


def labels(language: str) -> dict:
    return LABELS.get(language, LABELS["zh"])
