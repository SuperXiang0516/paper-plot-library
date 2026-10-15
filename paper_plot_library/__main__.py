import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from .config import load_settings
from .pipeline import import_seed, run_pipeline, validate_catalog
from .release_export import export_site
from .site import build_site
from .storage import read_json
from .sources import normalize_doi
from .session import run_backfill_session
from .offline_scope import refresh_scope_offline


def main(argv=None):
    parser = argparse.ArgumentParser(prog="paper-plot-library", description="论文绘图案例库：自动采集、静态检查、独立网站")
    default_root = Path.cwd() if (Path.cwd() / "web" / "index.html").exists() else Path(__file__).resolve().parent.parent
    parser.add_argument("--root", type=Path, default=default_root)
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed", help="首次导入已有人工核查案例（不覆盖已有库）")
    seed.add_argument("source", type=Path)
    run = sub.add_parser("update", help="有预算的完整自动更新；密钥缺失则降级")
    run.add_argument("--mode", choices=("auto", "backfill", "daily"), default="auto",
                     help="auto / daily 优先新增，不受旧材料缺口阻塞；backfill 仅补齐已有案例")
    run.add_argument("--no-discovery", action="store_true")
    run.add_argument("--doi", action="append", help="只修补指定已有 DOI")
    run.add_argument("--repair-limit", type=int)
    run.add_argument("--bootstrap-batch-limit", type=int, help="补库阶段每轮最多检查的既有案例数（1—50）")
    run.add_argument("--max-papers-per-run", type=int, help="本轮来源检查总篇数上限；补库、修补和新候选共享")
    run.add_argument("--max-ai-calls", type=int)
    run.add_argument("--max-requests", type=int)
    session = sub.add_parser("backfill-session", help="连续补齐现有案例：每批最多5篇，有限预算与逐批保存，不发现新文献")
    session.add_argument("--max-batches", type=int)
    session.add_argument("--session-max-ai-attempts", type=int)
    session.add_argument("--session-max-requests", type=int)
    session.add_argument("--max-seconds", type=int)
    session.add_argument("--new-session", action="store_true", help="显式开启下一轮固定队列复查；保留材料缺口与冷却规则")
    sub.add_parser("build", help="离线构建网站，不调用 API")
    sub.add_parser("export", help="白名单导出可发布文件；不带入预览目录中的用户文件")
    sub.add_parser("check", help="检查配置/数据；只报告密钥是否存在，不显示值")
    scope_refresh = sub.add_parser("scope-refresh", help="仅用已缓存正式元数据复核旧条目的研究方向；不抓取、不计新增")
    scope_refresh.add_argument("--offline", required=True, action="store_true", help="明确禁止网络、AI 与全文下载")
    serve = sub.add_parser("serve", help="仅监听本机的网页预览")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "seed":
        catalog = import_seed(root, args.source)
        print(f"已导入 {len(catalog['papers'])} 条人工种子；自动核查状态单独记录")
    elif args.command == "update":
        if args.doi and any(not normalize_doi(doi) for doi in args.doi):
            parser.error("--doi 必须为有效 DOI；未开始更新")
        overrides = {key: getattr(args, key) for key in ("repair_limit", "bootstrap_batch_limit", "max_papers_per_run", "max_ai_calls", "max_requests") if getattr(args, key) is not None}
        report = run_pipeline(root, load_settings(root, overrides), only_dois=args.doi,
                              discover_new=not args.no_discovery and not args.doi and args.mode != "backfill",
                              mode=args.mode)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "backfill-session":
        fields = {"max_batches": "session_max_batches", "session_max_ai_attempts": "session_max_ai_attempts",
                  "session_max_requests": "session_max_requests", "max_seconds": "session_max_seconds"}
        overrides = {field: getattr(args, argument) for argument, field in fields.items() if getattr(args, argument) is not None}
        summary = run_backfill_session(root, load_settings(root, overrides), new_session=args.new_session)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "build":
        output = build_site(root, validate_catalog(read_json(root / "data" / "catalog.json")), read_json(root / "data" / "updates.json", []))
        print("已构建：" + str(output))
    elif args.command == "export":
        print(str(export_site(root)))
    elif args.command == "check":
        settings = load_settings(root)
        catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
        print(json.dumps({"project": "paper-plot-library", "cases": len(catalog["papers"]), "authorCodeExecuted": False,
                          "configured": {name: bool(getattr(settings, name)) for name in ("llm_api_key", "llm_model", "easyscholar_key", "ncbi_api_key", "openalex_key", "github_token")},
                          "daily_new_cap": settings.max_new_per_day, "ai_attempt_cap": settings.max_ai_calls,
                          "paper_batch_limit": settings.max_papers_per_run,
                          "max_ai_attempts_per_paper": settings.max_ai_attempts_per_paper,
                          "initialize_existing_first": settings.initialize_existing_first,
                          "bootstrap_batch_limit": settings.bootstrap_batch_limit}, ensure_ascii=False, indent=2))
    elif args.command == "scope-refresh":
        print(json.dumps(refresh_scope_offline(root), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        if not 1024 <= args.port <= 65535:
            parser.error("port 必须在 1024—65535")
        build_site(root, validate_catalog(read_json(root / "data" / "catalog.json")), read_json(root / "data" / "updates.json", []))
        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(SimpleHTTPRequestHandler, directory=str(root / "site")))
        print(f"本机预览：http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()


if __name__ == "__main__":
    main()
