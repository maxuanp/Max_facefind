from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .config import Config
from .env import browser_status, gpu_status, python_status
from .face import FaceEngine, build_references
from .index import ImageIndex
from .net import HttpClient
from .people import discover_references
from .pipeline import RunOptions, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="facefind", description="按人脸 + 人名自动搜索并下载图片（百度图片已实现）"
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="环境勘察：Python/包/GPU/浏览器/网络")
    p_probe.add_argument("--skip-network", action="store_true", help="跳过百度连通性测试")

    p_faces = sub.add_parser("faces", help="检查参考照：发现规则、人脸检测告警")
    p_faces.add_argument("--faces-dir", help="参考照根目录（默认取配置 faces.dir）")
    p_faces.add_argument("--name", action="append", help="仅检查指定人名")
    p_faces.add_argument("--face", action="append", metavar="姓名=路径", help="显式指定参考照文件或目录")

    p_run = sub.add_parser("run", help="执行搜索下载")
    p_run.add_argument("--name", action="append", help="人名（可多次）；缺省处理 faces/ 下所有")
    p_run.add_argument("--count", type=int, help="每人本次目标收录数量")
    p_run.add_argument("--accept", type=float, help="直接收录阈值 A（相似度）")
    p_run.add_argument("--review", type=float, help="复核阈值 B；B<=sim<A 进 review/")
    p_run.add_argument("--mode", choices=["max", "mean", "centroid"], help="多参考照比对模式")
    p_run.add_argument("--sources", help="站点，逗号分隔：baidu,weibo,xiaohongshu")
    p_run.add_argument("--query", action="append", help="追加搜索关键词（可多次）")
    p_run.add_argument("--faces-dir", help="参考照根目录")
    p_run.add_argument("--face", action="append", metavar="姓名=路径", help="显式指定参考照")
    p_run.add_argument("--out", help="输出目录（默认 downloads）")
    p_run.add_argument("--proxy", help="HTTP 代理，例如 http://127.0.0.1:3080")
    p_run.add_argument("--no-face", action="store_true", help="关闭人脸过滤（仅按关键词抓图）")
    p_run.add_argument("--max-pages", type=int, help="每个关键词最多翻页数")
    p_run.add_argument("--min-width", type=int, help="最小宽度")
    p_run.add_argument("--min-height", type=int, help="最小高度")
    p_run.add_argument("--exclude-dir", action="append", help="排除目录（可多次，递归扫描）")
    p_run.add_argument("--exclude-file", action="append", help="排除文件（可多次）")
    p_run.add_argument("--exclude-threshold", type=int, help="排除 pHash 汉明距离阈值 0-16")
    p_run.add_argument("--exclude-rebuild", action="store_true", help="强制重建排除目录索引")
    p_run.add_argument("--min-face-px", type=int, help="最小人脸像素（默认取配置，建议 80）")
    p_run.add_argument("--profile", help="模型档位：balanced / max_quality")
    p_run.add_argument("--face-model", help="人脸模型：buffalo_l / antelopev2")
    p_run.add_argument("--framing-model", help="构图模型：yolov8n/m/x-pose / yolo11m/x-pose")
    p_run.add_argument("--det-size", type=int, help="人脸检测输入边长（默认取档位）")
    p_run.add_argument("--det-thresh", type=float, help="人脸检测阈值（默认取档位）")
    p_run.add_argument("--framing-mode", choices=["prefer_full_body", "allow_upper_body", "strict_full_body"],
                       help="构图策略（默认取配置 prefer_full_body）")
    p_run.add_argument("--edge-cut-tolerance", type=float, help="人物框距边缘容差（占画面比例，默认 0.03）")
    p_run.add_argument("--min-person-ratio", type=float, help="人物框最小高度占比（默认 0.45）")
    p_run.add_argument("--no-review", action="store_true", help="关闭 review：低于 A 的候选直接丢弃")

    sub.add_parser("stats", help="查看跨运行索引统计")

    p_ui = sub.add_parser("ui", help="启动本地网页界面（默认仅监听 127.0.0.1）")
    p_ui.add_argument("--host", default="127.0.0.1", help="监听地址（Docker 内用 0.0.0.0）")
    p_ui.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p_ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    p_login = sub.add_parser("login", help="打开浏览器登录窗口（小红书/微博）")
    p_login.add_argument("site", choices=["xiaohongshu", "weibo"])

    p_export = sub.add_parser("export", help="生成某人成品缩略图总览 HTML")
    p_export.add_argument("person", nargs="?", help="人名；缺省配合 --all")
    p_export.add_argument("--all", action="store_true", help="为所有有成品的人员生成")

    p_cal = sub.add_parser("calibrate", help="导出最近候选相似度并给出建议阈值")
    p_cal.add_argument("person", help="人名")
    p_cal.add_argument("--limit", type=int, default=100, help="最近 N 张（默认 100）")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    cfg = Config.load(args.config)

    if args.cmd == "probe":
        return cmd_probe(cfg, skip_network=args.skip_network)
    if args.cmd == "faces":
        return cmd_faces(cfg, args)
    if args.cmd == "run":
        return cmd_run(cfg, args)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    if args.cmd == "ui":
        return cmd_ui(cfg, args)
    if args.cmd == "login":
        return cmd_login(cfg, args.site)
    if args.cmd == "export":
        return cmd_export(cfg, args)
    if args.cmd == "calibrate":
        return cmd_calibrate(cfg, args)
    return 1


def cmd_probe(cfg: Config, skip_network: bool = False) -> int:
    print("== 运行环境 ==")
    for k, v in python_status().items():
        print(f"  {k:10s} {v}")

    print("\n== Python 依赖 ==")
    for mod in ("numpy", "cv2", "PIL", "requests", "yaml", "insightface", "onnxruntime", "torch", "sklearn"):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "installed")
        except Exception:
            ver = "MISSING"
        print(f"  {mod:12s} {ver}")

    gpu = gpu_status()
    print("\n== GPU / 推理后端 ==")
    print(f"  torch         {gpu.torch_version}  cuda_available={gpu.torch_cuda}  device={gpu.torch_device or '-'}")
    print(f"  onnxruntime   {gpu.ort_version}  providers={gpu.ort_providers}")
    print(f"  CUDA 可用     {gpu.gpu_usable}（insightface 将使用 "
          f"{'CUDAExecutionProvider' if gpu.gpu_usable else 'CPUExecutionProvider'}）")

    print("\n== 浏览器 / 媒体工具 ==")
    for k, v in browser_status().items():
        print(f"  {k:12s} {v}")

    models_dir = cfg.path(cfg.face.root) / "models"
    print(f"\n== insightface 模型（档位 {cfg.profile}）==")
    print(f"  人脸：{cfg.face.model}  det_size={cfg.face.det_size} det_thresh={cfg.face.det_thresh}")
    print(f"  构图：{cfg.framing.model}  input={cfg.framing.input_size}")
    if models_dir.is_dir():
        for d in sorted(models_dir.iterdir()):
            if d.is_dir():
                files = [f.name for f in d.glob("*.onnx")]
                print(f"  {d.name:16s} {len(files)} onnx {files}")
    else:
        print(f"  未找到 {models_dir}")

    if not skip_network:
        print("\n== 百度图片连通性 ==")
        client = HttpClient(cfg.network, "baidu")
        try:
            resp = client.get(
                "https://image.baidu.com/search/acjson",
                params={"tn": "resultjson_com", "ipn": "rj", "word": "测试", "pn": "0", "rn": "1", "gsm": "1e"},
                referer="https://image.baidu.com/",
            )
            print(f"  HTTP {resp.status_code}, 响应 {len(resp.text)} 字节，代理={cfg.network.proxy or '直连'}")
        except Exception as exc:
            print(f"  失败：{exc}（可尝试 --proxy http://127.0.0.1:3080）")
    return 0


def cmd_faces(cfg: Config, args) -> int:
    explicit = _parse_faces(args.face)
    refs = discover_references(cfg, args.name, explicit, args.faces_dir)
    if not refs:
        print("未发现参考照。请放到 faces/<人名>/ 下，或使用 --face 姓名=路径。")
        return 2
    root = cfg.face.root
    if root and not Path(root).is_absolute():
        root = str(cfg.path(root))
    engine = FaceEngine(
        model=cfg.face.model, device=cfg.face.device, det_size=cfg.face.det_size,
        det_thresh=cfg.face.det_thresh,
        min_face_px=cfg.face.min_face_px, allow_multiple_faces=cfg.face.allow_multiple_faces,
        root=root,
    ).load()
    print(f"设备：{engine.device}  档位：{cfg.profile}  模型：{engine.model}  模式：{cfg.face.similarity_mode}")
    print(f"阈值：accept={cfg.face.accept_threshold}  review={cfg.face.review_threshold}  "
          f"dedupe={cfg.face.dedupe_threshold}")
    for name, paths in refs.items():
        print(f"\n[{name}] 候选参考照 {len(paths)} 个")
        person_refs, warns, used = build_references(
            engine, name, paths, cfg.face.similarity_mode, max_refs=20
        )
        for w in warns:
            print("  ⚠ " + w)
        for p in used:
            print("  ✓ " + p)
        if person_refs is None:
            print("  ✗ 无可用参考照（该人会被跳过）")
        else:
            print(f"  可用 {len(person_refs.refs)} 张，比对模式={person_refs.mode}")
    return 0


def cmd_run(cfg: Config, args) -> int:
    explicit = _parse_faces(args.face)
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    if args.profile:
        cfg.apply_profile(args.profile)
    if args.face_model:
        cfg.face.model = str(args.face_model)
    if args.framing_model:
        cfg.framing.model = str(args.framing_model)
    if args.det_size is not None:
        cfg.face.det_size = max(64, int(args.det_size))
    if args.det_thresh is not None:
        cfg.face.det_thresh = max(0.0, min(1.0, float(args.det_thresh)))
    if args.min_face_px is not None:
        cfg.face.min_face_px = max(0, int(args.min_face_px))
    if args.framing_mode:
        cfg.framing.mode = args.framing_mode
    if args.edge_cut_tolerance is not None:
        cfg.framing.edge_cut_tolerance = max(0.0, min(0.2, float(args.edge_cut_tolerance)))
    if args.min_person_ratio is not None:
        cfg.framing.min_person_ratio = max(0.0, min(1.0, float(args.min_person_ratio)))
    opt = RunOptions(
        names=args.name,
        count=args.count,
        accept_threshold=args.accept,
        review_threshold=args.review,
        mode=args.mode,
        sources=sources,
        queries=args.query,
        faces_dir=args.faces_dir,
        explicit_faces=explicit,
        out_dir=args.out,
        use_face=False if args.no_face else None,
        max_pages=args.max_pages,
        min_width=args.min_width,
        min_height=args.min_height,
        proxy=args.proxy,
        exclude_dirs=args.exclude_dir or [],
        exclude_files=args.exclude_file or [],
        exclude_phash_threshold=args.exclude_threshold,
        exclude_rebuild=args.exclude_rebuild,
        review_enabled=False if args.no_review else None,
        on_log=print,
    )
    try:
        report = run(cfg, opt)
    except ValueError as exc:
        print(f"错误：{exc}")
        return 2
    print("\n== 汇总 ==")
    print(f"设备：{report.device}  总耗时：{report.elapsed}s")
    for p in report.people:
        print(f"  {p.person}: 本次新增 {p.saved_added}，review {p.review_added}，"
              f"替换 {p.replaced}，重复 {p.duplicates}，排除 {p.excluded}，"
              f"丢弃 {p.rejected}，失败 {p.failed}，累计 {p.saved_now}/{p.target}")
    totals = report.totals
    print("  合计：" + "，".join(f"{k}={v}" for k, v in totals.items()))
    print(f"  输出目录：{cfg.output_path}")
    print(f"  索引数据库：{cfg.index_path}")
    return 0


def cmd_stats(cfg: Config) -> int:
    index = ImageIndex(cfg.index_path)
    print(json.dumps(index.summary(), ensure_ascii=False, indent=2))
    return 0


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _facefind_running(host: str, port: int) -> bool:
    """端口上是否已有本工具的 UI 实例（避免重复启动直接报 10048）。"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=1.5) as resp:
            data = json.load(resp)
        return data.get("app") == "facefind"
    except Exception:
        return False


def cmd_ui(cfg: Config, args) -> int:
    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn：请执行 python -m pip install uvicorn")
        return 2
    from .server import create_app

    host = str(getattr(args, "host", "") or "127.0.0.1")
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    port = int(args.port)
    url = f"http://{check_host}:{port}/"

    if not _port_free(check_host, port):
        if _facefind_running(check_host, port):
            print(f"facefind UI 已在运行：{url}")
            print("  已复用现有实例，无需重复启动；直接使用浏览器即可。")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"端口 {port} 已被其它程序占用：{url}")
        print(f"  可换端口启动：python -m facefind ui --port {port + 1}")
        print(f"  或关闭占用程序后重试（Windows：netstat -ano | findstr :{port}）")
        return 3

    app = create_app(args.config)
    # 托盘宿主/管理接口需要一个可优雅退出的钩子：设置 server.should_exit 而不是 os._exit，
    # 这样 uvicorn 会执行 lifespan shutdown，watcher 线程与 Playwright 实例能正常收尾。
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=5,
    ))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    print(f"facefind UI: {url}  （仅监听 {host}，Ctrl+C 结束）")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server.run()
    return 0


def cmd_login(cfg: Config, site: str) -> int:
    from . import browser

    if not browser.playwright_installed():
        print("缺少 playwright：请执行 python -m pip install playwright")
        return 2
    print(f"正在打开 {site} 登录窗口，请手动完成登录（成功后会自动关闭）…")
    result = browser.open_login_window(site, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("logged_in") else 2


def cmd_export(cfg: Config, args) -> int:
    from .export import build_overview

    index = ImageIndex(cfg.index_path)
    names = [args.person] if args.person else (
        list(index.summary().get("by_person", {}).keys()) if args.all else []
    )
    if not names:
        print("请指定人名，或使用 --all")
        return 2
    for name in names:
        path = build_overview(name, cfg, index)
        print(f"已生成：{path}")
    return 0


def cmd_calibrate(cfg: Config, args) -> int:
    from .calibrate import collect

    data = collect(args.person, cfg, limit=args.limit)
    s = data["suggestion"]
    print(f"人员：{data['person']}  候选：{len(data['records'])} 张  CSV：{data['csv_path']}")
    print(f"建议阈值：accept >= {s['accept_threshold']}，review >= {s['review_threshold']}"
          f"（当前 A={s['current']['accept']} B={s['current']['review']}）")
    print(f"样本：正 {s['positive_n']} / 负 {s['negative_n']}")
    if s["note"]:
        print(s["note"])
    return 0


def _parse_faces(values: list[str] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw in values or []:
        if "=" not in raw:
            raise SystemExit(f"--face 需要 姓名=路径 形式，收到：{raw}")
        name, path = raw.split("=", 1)
        out.setdefault(name.strip(), []).append(path.strip())
    return out


if __name__ == "__main__":
    sys.exit(main())
