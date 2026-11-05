"""下载 facefind 需要的公开模型权重（不随仓库分发）。

用法：
    python scripts/fetch_models.py                     # 默认下载 buffalo_l
    python scripts/fetch_models.py --insightface buffalo_l,antelopev2 --sr base
    python scripts/fetch_models.py --sr all            # 追加 anime / gfpgan
    python scripts/fetch_models.py --list              # 只打印地址

说明：
- InsightFace 官方模型仅限非商业研究用途，见 LICENSE。
- 姿态模型（yolov8n-pose.onnx）没有稳定的官方直链，脚本只打印制作方式。
- SwinIR / HAT / SUPIR 权重体积大、来源分散，脚本不自动下载，只打印出处。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSIGHT_ROOT = ROOT / "models" / "insightface"
SR_DIR = ROOT / "models" / "sr"

INSIGHTFACE = {
    "buffalo_l": "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
    "antelopev2": "https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip",
}

# 目标文件名必须与 facefind/sr.py 里的 filename 一致
SR_BASE = {
    "realesrgan_x4plus.pth": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
    ),
    "gfpgan_v1.4.pth": (
        "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth"
    ),
}
SR_EXTRA = {
    "realesrgan_x4plus_anime_6b.pth": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"
    ),
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) facefind-model-fetch/1.0"}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[skip] {dest.name} 已存在（{dest.stat().st_size / 2**20:.1f} MB）")
        return
    print(f"[get ] {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct % 5 == 0:
                    print(f"       {pct:3d}%  {done / 2**20:.1f}/{total / 2**20:.1f} MB", end="\r")
            else:
                print(f"       {done / 2**20:.1f} MB", end="\r")
    tmp.replace(dest)
    print(f"\n[ok  ] {dest}  {dest.stat().st_size / 2**20:.1f} MB")


def fetch_insightface(name: str) -> None:
    url = INSIGHTFACE.get(name)
    if not url:
        print(f"[err ] 未知 InsightFace 模型：{name}（可选：{', '.join(INSIGHTFACE)}）")
        return
    target_dir = INSIGHT_ROOT / "models" / name
    if target_dir.exists() and any(target_dir.glob("*.onnx")):
        print(f"[skip] {name} 已解压：{target_dir}")
        return
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / f"{name}.zip"
        _download(url, zpath)
        print(f"[unzip] -> {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                zf.extract(member, target_dir)
    print(f"[ok  ] {name} 就绪：{target_dir}")


def fetch_sr(which: str) -> None:
    items: dict[str, str] = {}
    if which in ("base", "all"):
        items.update(SR_BASE)
    if which == "all":
        items.update(SR_EXTRA)
    if not items:
        return
    for filename, url in items.items():
        _download(url, SR_DIR / filename)


def print_optional() -> None:
    print(
        "\n可选模型（不自动下载）：\n"
        "  姿态构图 yolov8n-pose.onnx：\n"
        "    pip install ultralytics\n"
        "    yolo export model=yolov8n-pose.pt format=onnx imgsz=640\n"
        "    把生成的 yolov8n-pose.onnx 放到 models/ 目录。\n"
        "    缺失时构图过滤会保守放行（见 facefind/framing.py）。\n"
        "  SwinIR-L / HAT / SUPIR 超分权重：见 facefind/sr.py 中每个模型的 source 字段，\n"
        "    下载后按 sr.py 的 filename 命名放到 models/sr/。\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载 facefind 模型权重")
    ap.add_argument("--insightface", default="buffalo_l",
                    help="逗号分隔：buffalo_l / antelopev2 / none（默认 buffalo_l）")
    ap.add_argument("--sr", default="none", choices=["none", "base", "all"],
                    help="超分权重：none / base（Real-ESRGAN x4plus + GFPGAN）/ all（再加 anime）")
    ap.add_argument("--list", action="store_true", help="只打印下载地址")
    args = ap.parse_args(argv)

    if args.list:
        for k, v in {**INSIGHTFACE, **SR_BASE, **SR_EXTRA}.items():
            print(f"{k:38s} {v}")
        print_optional()
        return 0

    names = [n.strip() for n in (args.insightface or "").split(",") if n.strip()]
    for n in names:
        if n.lower() == "none":
            continue
        fetch_insightface(n)
    fetch_sr(args.sr)
    print_optional()
    return 0


if __name__ == "__main__":
    sys.exit(main())
