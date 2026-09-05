"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

from .config import Config
from .render import render_text
from .runner import run

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    if args.output_dir:
        config.data.setdefault("output", {})["dir"] = args.output_dir
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None

    report, written = run(
        config,
        today=date.fromisoformat(args.date) if args.date else None,
        dedup=not args.no_dedup,
        only=only,
        notify=not args.no_notify,
        write_files=not args.dry_run,
    )

    print(render_text(report, config.get("output.language", "zh"), per_section=8))
    if written:
        print("\n写入文件:")
        for path in written:
            print(f"  {path}")
    failed = [s for s in report.statuses if not s.ok]
    if failed:
        print("\n失败的数据源:", file=sys.stderr)
        for status in failed:
            print(f"  {status.name}: {status.message}", file=sys.stderr)
        return 1 if len(failed) == len(report.statuses) else 0
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} 已存在，使用 --force 覆盖", file=sys.stderr)
        return 1
    if EXAMPLE.exists():
        shutil.copyfile(EXAMPLE, target)
    else:
        # Installed as a package without the example file alongside it: emit the defaults.
        import yaml

        from .config import DEFAULTS

        target.write_text(
            "# Stock Radar 配置（由 defaults 生成）\n"
            "# sec.user_agent 必填：SEC 要求带可联系到你的邮箱。\n\n"
            + yaml.safe_dump(DEFAULTS, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    print(f"已生成 {target}，请填写 sec.user_agent（SEC 要求带联系邮箱）和 watchlist。")
    return 0


def cmd_find_fund(args: argparse.Namespace) -> int:
    from .edgar import Edgar
    from .http import Http

    config = Config.load(args.config) if Path(args.config).exists() else Config.load(None)
    http = Http(user_agent=config.user_agent or None, rate_per_sec=float(config.get("sec.rate_per_sec", 5.0)))
    try:
        matches = Edgar(http).search_companies(args.name, args.form)
    except Exception as exc:
        print(f"查询 EDGAR 失败: {exc}", file=sys.stderr)
        return 1
    if not matches:
        print(f"没有找到匹配 '{args.name}' 的 {args.form} 申报人", file=sys.stderr)
        return 1
    print(f"匹配 '{args.name}' 的申报人（把 cik 填进 config.yaml 的 watchlist.funds）:\n")
    for cik, name in matches:
        print(f'  - {{name: "{name}", cik: "{cik}"}}')
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .probe import main as probe_main

    config = Config.load(args.config) if Path(args.config).exists() else Config.load(None)
    return probe_main(
        config,
        today=date.fromisoformat(args.date) if args.date else None,
        verbose=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-radar",
        description="每日汇总国会议员交易、公司高管买卖、基金持仓变化和重要新闻。",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="抓取并生成今日报告")
    p_run.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    p_run.add_argument("--date", help="以指定日期运行 (YYYY-MM-DD)，用于回补")
    p_run.add_argument("--only", help="只跑部分采集器，逗号分隔: congress,insiders,funds,news")
    p_run.add_argument("--no-dedup", action="store_true", help="不做跨天去重（调试用）")
    p_run.add_argument("--no-notify", action="store_true", help="不发送推送")
    p_run.add_argument("--dry-run", action="store_true", help="不写文件")
    p_run.add_argument("--output-dir", help="覆盖 output.dir")
    p_run.set_defaults(func=cmd_run)

    p_init = sub.add_parser("init", help="生成配置文件模板")
    p_init.add_argument("path", nargs="?", default="config.yaml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_diag = sub.add_parser("diagnose", help="深度诊断：SEC 准入矩阵、国会数据源存活情况")
    p_diag.set_defaults(func=lambda a: (__import__("stock_radar.diagnose", fromlist=["run_all"]).run_all(), 0)[1])

    p_probe = sub.add_parser("probe", help="体检：逐个打真实数据源，检查格式假设是否还成立")
    p_probe.add_argument("-c", "--config", default="config.yaml")
    p_probe.add_argument("--date", help="以指定日期运行 (YYYY-MM-DD)")
    p_probe.set_defaults(func=cmd_probe)

    p_find = sub.add_parser("find-fund", help="按名称在 EDGAR 查询基金的 CIK")
    p_find.add_argument("name", help="基金名称关键字，如 berkshire")
    p_find.add_argument("-c", "--config", default="config.yaml")
    p_find.add_argument("--form", default="13F-HR", help="按申报类型过滤，默认 13F-HR")
    p_find.set_defaults(func=cmd_find_fund)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
