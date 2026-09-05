"""One-off access diagnostics for the endpoints that are known to be picky.

EDGAR rejects clients it considers anonymous, and the congressional data
mirrors come and go, so these routines answer "what exactly do they want?"
rather than only "did it work?".
"""

from __future__ import annotations

import json
import re
from typing import Iterable

import requests

TIMEOUT = 30

# EDGAR canaries: small, cacheable, and representative of what the collectors need.
SEC_CANARIES = [
    ("company_tickers", "https://www.sec.gov/files/company_tickers.json"),
    ("daily-index json", "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/index.json"),
    ("submissions API", "https://data.sec.gov/submissions/CIK0000320193.json"),
    ("browse-edgar atom", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=4&count=1&output=atom"),
    ("full-text search", "https://efts.sec.gov/LATEST/search-index?q=%22apple%22&forms=4"),
    ("press RSS (control)", "https://www.sec.gov/news/pressreleases.rss"),
]

# Header candidates, ordered from what SEC documents to what a browser sends.
HEADER_MATRIX: list[tuple[str, dict[str, str]]] = [
    (
        "SEC 文档写法: 名称+邮箱",
        {
            "User-Agent": "Stock Radar stock-radar@users.noreply.github.com",
            "Accept-Encoding": "gzip, deflate",
        },
    ),
    (
        "名称+邮箱 + Accept + Host",
        {
            "User-Agent": "Stock Radar stock-radar@users.noreply.github.com",
            "Accept": "application/json, text/html, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ),
    (
        "只有邮箱",
        {"User-Agent": "stock-radar@users.noreply.github.com", "Accept-Encoding": "gzip, deflate"},
    ),
    (
        "URL 作为联系方式（当前默认）",
        {
            "User-Agent": "stock-radar probe (+https://github.com/wander2001/design)",
            "Accept-Encoding": "gzip, deflate",
        },
    ),
    (
        "浏览器 UA",
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ),
    ("无 User-Agent", {"Accept-Encoding": "gzip, deflate"}),
]


def _probe_once(url: str, headers: dict[str, str]) -> str:
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as exc:
        return f"ERR {type(exc).__name__}: {str(exc)[:80]}"
    body = resp.content[:120].decode("utf-8", errors="replace").replace("\n", " ")
    return f"{resp.status_code} ({len(resp.content):,}B) {body[:90]!r}"


def sec_access_matrix(out=print) -> None:
    out("\n" + "=" * 78)
    out("== SEC 准入矩阵：哪种 User-Agent / 请求头能过 EDGAR")
    out("=" * 78)
    for label, headers in HEADER_MATRIX:
        out(f"\n--- {label}")
        out(f"    headers={json.dumps(headers, ensure_ascii=False)}")
        for name, url in SEC_CANARIES:
            out(f"    {name:22} → {_probe_once(url, headers)}")


def house_clerk_pdf_check(out=print) -> None:
    """The report links straight to a PTR PDF, so the URL pattern must be right."""
    out("\n" + "=" * 78)
    out("== 众议院 PTR PDF 链接格式验证")
    out("=" * 78)
    headers = {"User-Agent": "Stock Radar stock-radar@users.noreply.github.com"}
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    for year in (2026, 2025):
        url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
        try:
            raw = requests.get(url, headers=headers, timeout=TIMEOUT).content
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = zf.namelist()
                xml_bytes = next((zf.read(n) for n in names if n.lower().endswith(".xml")), None)
        except Exception as exc:
            out(f"  {year}FD.ZIP → ERR {type(exc).__name__}: {str(exc)[:90]}")
            continue
        if not xml_bytes:
            out(f"  {year}FD.ZIP → 没有 XML，压缩包内容: {names}")
            continue
        root = ET.fromstring(xml_bytes)
        members = [m for m in root.iter("Member") if (m.findtext("FilingType") or "").strip() == "P"]
        out(f"  {year}FD.ZIP → {len(raw):,}B，内含 {names}，PTR {len(members)} 条")
        for member in members[-3:]:
            fields = {c.tag: (c.text or "").strip() for c in member}
            doc_id, doc_year = fields.get("DocID", ""), fields.get("Year", str(year))
            for pattern in (
                f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{doc_year}/{doc_id}.pdf",
                f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf",
            ):
                try:
                    r = requests.head(pattern, headers=headers, timeout=TIMEOUT, allow_redirects=True)
                    out(f"    {fields.get('Last','?'):14} {pattern.rsplit('/public_disc/',1)[1]:34} → {r.status_code} {r.headers.get('content-type','')}")
                except Exception as exc:
                    out(f"    {pattern} → ERR {exc}")


def senate_efd_check(out=print) -> None:
    """Senate EFD gates its search behind a click-through agreement plus CSRF."""
    out("\n" + "=" * 78)
    out("== 参议院 EFD 搜索接口可行性")
    out("=" * 78)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Stock Radar stock-radar@users.noreply.github.com",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
    )
    home = "https://efdsearch.senate.gov/search/home/"
    try:
        page = session.get(home, timeout=TIMEOUT)
        out(f"  GET home → {page.status_code} ({len(page.content):,}B), cookies={list(session.cookies.keys())}")
    except Exception as exc:
        out(f"  GET home → ERR {type(exc).__name__}: {str(exc)[:120]}")
        return

    token = ""
    match = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)", page.text)
    if match:
        token = match.group(1)
    out(f"  csrfmiddlewaretoken: {'找到 ' + token[:12] + '…' if token else '没找到'}")

    try:
        accepted = session.post(
            home,
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
            headers={"Referer": home},
            timeout=TIMEOUT,
        )
        out(f"  POST 同意条款 → {accepted.status_code}, cookies={list(session.cookies.keys())}")
    except Exception as exc:
        out(f"  POST 同意条款 → ERR {type(exc).__name__}: {str(exc)[:120]}")
        return

    data_url = "https://efdsearch.senate.gov/search/report/data/"
    payload = {
        "start": "0",
        "length": "25",
        "report_types": "[11]",          # Periodic Transaction Reports
        "filer_types": "[]",
        "submitted_start_date": "01/01/2026 00:00:00",
        "submitted_end_date": "",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": token,
    }
    try:
        resp = session.post(data_url, data=payload, headers={"Referer": home}, timeout=TIMEOUT)
        out(f"  POST 搜索 → {resp.status_code} ({len(resp.content):,}B)")
        if resp.ok:
            try:
                body = resp.json()
                out(f"  返回键: {sorted(body)}")
                rows = body.get("data") or []
                out(f"  记录数: {len(rows)}；总数: {body.get('recordsTotal')}")
                for row in rows[:3]:
                    out(f"    行: {row}")
            except ValueError:
                out(f"  不是 JSON，前 300 字节: {resp.content[:300]!r}")
        else:
            out(f"  响应前 300 字节: {resp.content[:300]!r}")
    except Exception as exc:
        out(f"  POST 搜索 → ERR {type(exc).__name__}: {str(exc)[:120]}")


def congress_mirror_check(out=print) -> None:
    """The community mirrors moved or went private before; find out which still serve."""
    out("\n" + "=" * 78)
    out("== 国会交易数据镜像存活情况")
    out("=" * 78)
    headers = {"User-Agent": "Stock Radar stock-radar@users.noreply.github.com"}
    candidates = [
        "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
        "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
        "https://housestockwatcher.com/api",
        "https://senatestockwatcher.com/api",
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
        "https://api.quiverquant.com/beta/live/congresstrading",
        "https://www.capitoltrades.com/trades",
        "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.ZIP",
    ]
    for url in candidates:
        out(f"  {url[:88]:88} → {_probe_once(url, headers)}")


def run_all(out=print) -> None:
    sec_access_matrix(out)
    congress_mirror_check(out)
    house_clerk_pdf_check(out)
    senate_efd_check(out)
