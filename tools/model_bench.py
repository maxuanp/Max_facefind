"""模型量化对比工具（真实项目数据）。

用法：
  python tools/model_bench.py face      # 人脸：检出率/相似度/耗时/显存，每个配置独立子进程
  python tools/model_bench.py framing   # 构图：读取人工标注 JSON，对比多个姿态模型
  python tools/model_bench.py sheet     # 生成人工标注用的联系图
"""
from __future__ import annotations

import gc
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
REPORT_DIR = ROOT / "data" / "reports"
FACE_CONFIGS = [
    {"model": "buffalo_l", "det_size": 640, "det_thresh": 0.50},
    {"model": "buffalo_l", "det_size": 640, "det_thresh": 0.35},
    {"model": "buffalo_l", "det_size": 1024, "det_thresh": 0.50},
    {"model": "antelopev2", "det_size": 640, "det_thresh": 0.50},
    {"model": "antelopev2", "det_size": 640, "det_thresh": 0.35},
    {"model": "antelopev2", "det_size": 1024, "det_thresh": 0.35},
]
FRAMING_MODELS = ["yolov8n-pose", "yolov8m-pose", "yolov8x-pose", "yolo11m-pose", "yolo11x-pose"]


def imread(path: Path) -> np.ndarray | None:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def images_in(d: Path) -> list[Path]:
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.suffix.lower() in IMG_EXT and p.is_file())


def person_dirs(base: Path, exclude: tuple[str, ...] = ()) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name not in exclude:
            imgs = images_in(d)
            if imgs:
                out[d.name] = imgs
    return out


def dataset() -> dict:
    downloads = person_dirs(ROOT / "downloads", exclude=("review",))
    review = images_in(ROOT / "downloads" / "review")
    discarded = images_in(ROOT / "_raw" / "discarded")
    raw_baidu = images_in(ROOT / "_raw" / "baidu")
    rng = random.Random(20261105)
    sample_raw = rng.sample(raw_baidu, min(60, len(raw_baidu)))
    if review:
        sample_raw = sample_raw[: max(0, 60 - len(review))] + review
    all_det = [p for imgs in downloads.values() for p in imgs] + sample_raw + discarded
    return {"downloads": downloads, "discarded": discarded, "sample_raw": sample_raw,
            "review": review, "det_images": all_det}


def gpu_mem_mb() -> float:
    try:
        import pynvml

        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(h)
        return info.used / 1024 / 1024
    except Exception:
        return -1.0


def _pct(v: list[float], q: float) -> float:
    if not v:
        return 0.0
    v = sorted(v)
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] * (1 - pos + lo) + v[hi] * (pos - lo)


def _auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return 0.0
    total = 0.0
    for p in pos:
        for n in neg:
            total += 1.0 if p > n else (0.5 if p == n else 0.0)
    return total / (len(pos) * len(neg))


# ---------------- 人脸：单个配置（子进程） ----------------
def face_one(cfg_dict: dict) -> dict:
    from facefind.face import FaceEngine

    cfg = {
        "model": cfg_dict["model"], "det_size": cfg_dict["det_size"],
        "det_thresh": cfg_dict["det_thresh"], "min_face_px": 80,
    }
    data = dataset()
    paths = data["det_images"]
    imgs: list[np.ndarray] = []
    valid: list[Path] = []
    for p in paths:
        im = imread(p)
        if im is not None:
            imgs.append(im)
            valid.append(p)
    base = gpu_mem_mb()
    t0 = time.time()
    engine = FaceEngine(model=cfg["model"], device="auto", det_size=cfg["det_size"],
                        det_thresh=cfg["det_thresh"], root=str(ROOT / "models" / "insightface")).load()
    load_s = time.time() - t0
    peak = gpu_mem_mb()
    n_faces, face_px, times = [], [], []
    for im in imgs[:3]:
        engine.detect(im)
    for im in imgs:
        t = time.perf_counter()
        fs = engine.detect(im)
        times.append((time.perf_counter() - t) * 1000)
        n_faces.append(len(fs))
        if fs:
            biggest = max(fs, key=lambda x: float((x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])))
            face_px.append(max(float(biggest.bbox[2] - biggest.bbox[0]),
                               float(biggest.bbox[3] - biggest.bbox[1])))
        else:
            face_px.append(0.0)
        peak = max(peak, gpu_mem_mb())

    # 构图标记为 full_body 的子集（项目主要场景）
    fb_paths: list[Path] = []
    try:
        conn = sqlite3.connect(str(ROOT / "data" / "index.db"))
        for fp, rp in conn.execute("SELECT file_path, raw_path FROM images WHERE framing='full_body'"):
            p = Path(fp) if fp else (Path(rp) if rp else None)
            if p and p.exists() and p.suffix.lower() in IMG_EXT:
                fb_paths.append(p)
        conn.close()
    except Exception:
        pass
    fb = [imread(p) for p in fb_paths[:120]]
    fb = [im for im in fb if im is not None]
    fb_rate = (sum(1 for im in fb if len(engine.detect(im)) >= 1) / len(fb)) if fb else 0.0

    # 相似度：以目录归属为真值（同人成品为正样本，异人/丢弃为负样本）
    refs_by_person: dict[str, list[np.ndarray]] = {}
    for name in data["downloads"]:
        fdir = ROOT / "faces" / name
        refs = []
        for p in images_in(fdir)[:20]:
            im = imread(p)
            if im is None:
                continue
            emb, match = engine.embed_largest(im)
            if emb is not None and match.reason != "face_too_small":
                refs.append(emb)
        if refs:
            refs_by_person[name] = refs

    pos_sims: dict[str, list[float]] = {}
    neg_sims: dict[str, list[float]] = {}
    loo_pos: dict[str, list[float]] = {}
    for name, refs in refs_by_person.items():
        # 参考照留一法正样本（无偏）
        for i, emb in enumerate(refs):
            others = [r for j, r in enumerate(refs) if j != i]
            if others:
                loo_pos.setdefault(name, []).append(max(float(np.dot(r, emb)) for r in others))
        pv = []
        for p in data["downloads"][name]:
            im = imread(p)
            if im is None:
                continue
            emb, _ = engine.embed_largest(im)
            if emb is not None:
                pv.append(max(float(np.dot(r, emb)) for r in refs))
        pos_sims[name] = pv
        nv = []
        others = [x for x in data["downloads"] if x != name]
        neg_paths = [p for x in others for p in data["downloads"][x]] + data["discarded"] + data["review"]
        for p in neg_paths:
            if p in data["downloads"].get(name, []):
                continue
            im = imread(p)
            if im is None:
                continue
            emb, _ = engine.embed_largest(im)
            if emb is not None:
                nv.append(max(float(np.dot(r, emb)) for r in refs))
        neg_sims[name] = nv

    all_pos = [s for v in pos_sims.values() for s in v]
    all_neg = [s for v in neg_sims.values() for s in v]
    all_loo = [s for v in loo_pos.values() for s in v]
    res = {
        "model": cfg["model"], "det_size": cfg["det_size"], "det_thresh": cfg["det_thresh"],
        "device": engine.device,
        "load_s": round(load_s, 2),
        "n_images": len(imgs),
        "det_rate": round(sum(1 for n in n_faces if n >= 1) / max(1, len(n_faces)), 4),
        "det_rate_px80": round(sum(1 for px in face_px if px >= 80) / max(1, len(face_px)), 4),
        "n_faces_avg": round(float(np.mean(n_faces)), 3),
        "face_px_p10": round(_pct([p for p in face_px if p > 0], 10), 1),
        "face_px_p50": round(_pct([p for p in face_px if p > 0], 50), 1),
        "face_px_p90": round(_pct([p for p in face_px if p > 0], 90), 1),
        "small_face_share": round(sum(1 for p in face_px if 0 < p < 80) / max(1, sum(1 for p in face_px if p > 0)), 4),
        "fullbody_subset_n": len(fb),
        "fullbody_subset_det_rate": round(fb_rate, 4),
        "det_ms_mean": round(statistics.mean(times), 2) if times else 0,
        "det_ms_p95": round(_pct(times, 95), 2),
        "gpu_base_mb": round(base, 1), "gpu_peak_mb": round(peak, 1),
        "gpu_delta_mb": round(peak - base, 1),
        "sim": {
            "pos_n": len(all_pos), "neg_n": len(all_neg),
            "pos_mean": round(statistics.mean(all_pos), 4) if all_pos else 0,
            "pos_p10": round(_pct(all_pos, 10), 4),
            "neg_mean": round(statistics.mean(all_neg), 4) if all_neg else 0,
            "neg_p90": round(_pct(all_neg, 90), 4),
            "neg_max": round(max(all_neg), 4) if all_neg else 0,
            "gap_p10_p90": round(_pct(all_pos, 10) - _pct(all_neg, 90), 4),
            "auc": round(_auc(all_pos, all_neg), 4),
            "loo_pos_n": len(all_loo),
            "loo_pos_mean": round(statistics.mean(all_loo), 4) if all_loo else 0,
            "loo_pos_p10": round(_pct(all_loo, 10), 4),
            "loo_minus_negp90": round(_pct(all_loo, 10) - _pct(all_neg, 90), 4),
            "pos_list": [round(x, 4) for x in all_pos],
            "neg_list": [round(x, 4) for x in all_neg],
        },
        "per_person": {
            "pos_mean": {k: round(statistics.mean(v), 4) if v else 0 for k, v in pos_sims.items()},
            "neg_mean": {k: round(statistics.mean(v), 4) if v else 0 for k, v in neg_sims.items()},
        },
    }
    del engine
    gc.collect()
    return res


# ---------------- 远景/全身小脸子集 ----------------
def far_select() -> dict:
    """用姿态模型（yolo11x）单人物+头部关键点可见+人物占比偏低筛选远景全身图。

    选择过程不依赖人脸模型，避免用 buffalo/antelope 的检测结果自证。
    """
    import math

    from facefind.config import Config
    from facefind.framing import _persons

    cfg = Config.load(ROOT / "config.yaml")
    cfg.framing.model = "yolo11x-pose"
    cfg.framing.input_size = 640
    data = dataset()
    pool = data["sample_raw"] + [p for imgs in data["downloads"].values() for p in imgs]
    pool += images_in(ROOT / "_raw" / "baidu")
    pool = list(dict.fromkeys(pool))
    picked = []
    for p in pool:
        im = imread(p)
        if im is None:
            continue
        h, w = im.shape[:2]
        persons = _persons(im, cfg)
        if len(persons) != 1:
            continue
        per = persons[0]
        ratio = (per["box"][3] - per["box"][1]) / h
        if not (0.25 <= ratio <= 0.80):
            continue
        head = [per["kpts"][i] for i in (0, 1, 2, 3, 4, 5)]  # nose/eyes/ears
        vis = [k for k in head if k[2] >= 0.5]
        if len(vis) < 2:
            continue
        ys = [k[1] for k in vis]
        if min(ys) < 0.02 * h:
            continue  # 头顶/脸部被上边缘切断
        eyes = [per["kpts"][1], per["kpts"][2]]
        if all(k[2] >= 0.5 for k in eyes):
            eye_px = math.hypot(eyes[0][0] - eyes[1][0], eyes[0][1] - eyes[1][1])
            face_px = eye_px * 1.7
        else:
            face_px = (per["box"][2] - per["box"][0]) * 0.18
        picked.append({"path": str(p.relative_to(ROOT)), "person_ratio": round(ratio, 3),
                       "face_px_est": round(face_px, 1), "y2": round(per["box"][3] / h, 3)})
    picked.sort(key=lambda x: x["face_px_est"])
    out = REPORT_DIR / "far_samples.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"远景/全身候选 {len(picked)} 张 -> {out}")
    return {"samples": picked}


def face_far_one(cfg_dict: dict) -> dict:
    from facefind.face import FaceEngine

    samples = json.loads((REPORT_DIR / "far_samples.json").read_text(encoding="utf-8"))
    engine = FaceEngine(model=cfg_dict["model"], device="auto", det_size=cfg_dict["det_size"],
                        det_thresh=cfg_dict["det_thresh"], root=str(ROOT / "models" / "insightface")).load()
    det, px80, px, times, misses = 0, 0, [], [], []
    for s in samples:
        im = imread(ROOT / s["path"])
        if im is None:
            continue
        t = time.perf_counter()
        fs = engine.detect(im)
        times.append((time.perf_counter() - t) * 1000)
        if fs:
            det += 1
            big = max(fs, key=lambda x: float((x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])))
            p = max(float(big.bbox[2] - big.bbox[0]), float(big.bbox[3] - big.bbox[1]))
            px.append(p)
            if p >= 80:
                px80 += 1
        else:
            misses.append(s["path"])
            px.append(0.0)
    n = max(1, len(samples))
    return {
        "model": cfg_dict["model"], "det_size": cfg_dict["det_size"], "det_thresh": cfg_dict["det_thresh"],
        "n": len(samples), "det_rate": round(det / n, 4), "det_rate_px80": round(px80 / n, 4),
        "face_px_p10": round(_pct([x for x in px if x > 0], 10), 1),
        "face_px_p50": round(_pct([x for x in px if x > 0], 50), 1),
        "ms_mean": round(statistics.mean(times), 2),
        "misses": misses,
    }


def thr_suite() -> dict:
    """对两个主配置做阈值扫描（TPR/FPR），给出推荐 accept/review。"""
    rows = []
    for cfg in (FACE_CONFIGS[0], FACE_CONFIGS[3]):
        cmd = [sys.executable, str(Path(__file__).resolve()), "face-one",
               cfg["model"], str(cfg["det_size"]), str(cfg["det_thresh"])]
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("{")), "")
        if not line:
            print(f"阈值配置失败 {cfg}: {out.stderr[-300:]}")
            continue
        r = json.loads(line)
        pos, neg = r["sim"]["pos_list"], r["sim"]["neg_list"]
        curve = []
        for t in (0.65, 0.60, 0.58, 0.55, 0.52, 0.50, 0.45, 0.40, 0.35, 0.30):
            tpr = sum(1 for x in pos if x >= t) / max(1, len(pos))
            fpr = sum(1 for x in neg if x >= t) / max(1, len(neg))
            curve.append({"thr": t, "tpr": round(tpr, 4), "fpr": round(fpr, 4),
                          "precision_est": round(tpr / max(1e-9, tpr + fpr * 0.9), 4)})
        rows.append({"model": cfg["model"], "n_pos": len(pos), "n_neg": len(neg),
                     "neg_p95": round(_pct(neg, 95), 4), "pos_p05": round(_pct(pos, 5), 4),
                     "curve": curve})
        print(f"阈值完成 {cfg['model']}")
    report = {"kind": "threshold", "rows": rows}
    _save(report, "threshold")
    return report


def far_suite() -> dict:
    far_select()
    rows = []
    for cfg in FACE_CONFIGS:
        cmd = [sys.executable, str(Path(__file__).resolve()), "far-one",
               cfg["model"], str(cfg["det_size"]), str(cfg["det_thresh"])]
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("{")), "")
        if not line:
            print(f"远景子集配置失败 {cfg}: {out.stderr[-300:]}")
            continue
        rows.append(json.loads(line))
        print(f"远景完成 {cfg['model']} det={cfg['det_size']} th={cfg['det_thresh']}")
    report = {"kind": "far", "rows": rows}
    _save(report, "far")
    return report


# ---------------- 人脸：编排 ----------------
def face_suite() -> dict:
    rows = []
    for cfg in FACE_CONFIGS:
        cmd = [sys.executable, str(Path(__file__).resolve()), "face-one",
               cfg["model"], str(cfg["det_size"]), str(cfg["det_thresh"])]
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("{")), "")
        if not line:
            print(f"配置失败 {cfg}: {out.stderr[-500:]}")
            continue
        rows.append(json.loads(line))
        print(f"完成 {cfg['model']} det={cfg['det_size']} th={cfg['det_thresh']}")
    report = {"kind": "face", "rows": rows}
    _save(report, "face")
    return report


# ---------------- 构图 ----------------
def sheet() -> Path:
    """生成人工标注联系图（6 列，编号）。"""
    data = dataset()
    picks: list[Path] = []
    # 取成品目录里前两个人名做样本（不写死具体人名）
    names = sorted(data["downloads"].keys())[:2]
    if len(names) >= 1:
        picks += data["downloads"][names[0]][:8]
    if len(names) >= 2:
        picks += data["downloads"][names[1]][:12]
    picks += data["review"] + data["discarded"][:4]
    picks += data["sample_raw"][:10]
    picks = list(dict.fromkeys(picks))[:34]
    thumbs = []
    H, W = 360, 270
    for i, p in enumerate(picks):
        im = imread(p)
        if im is None:
            continue
        h, w = im.shape[:2]
        s = min(W / w, H / h)
        t = cv2.resize(im, (max(1, int(w * s)), max(1, int(h * s))))
        canvas = np.full((H, W, 3), 30, np.uint8)
        y, x = (H - t.shape[0]) // 2, (W - t.shape[1]) // 2
        canvas[y:y + t.shape[0], x:x + t.shape[1]] = t
        cv2.putText(canvas, str(i), (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
        cv2.putText(canvas, str(i), (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 1)
        thumbs.append(canvas)
    cols = 6
    rows = (len(thumbs) + cols - 1) // cols
    grid = np.full((rows * H, cols * W, 3), 30, np.uint8)
    for i, t in enumerate(thumbs):
        r, c = divmod(i, cols)
        grid[r * H:(r + 1) * H, c * W:(c + 1) * W] = t
    out = REPORT_DIR / "framing_sheet.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(out))
    meta = REPORT_DIR / "framing_sheet_paths.json"
    meta.write_text(json.dumps([str(p.relative_to(ROOT)) for p in picks], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(out)
    return out


def framing_suite(labels_path: str | None = None) -> dict:
    from facefind.config import Config
    from facefind.framing import classify

    lp = Path(labels_path) if labels_path else (REPORT_DIR / "framing_labels.json")
    labels = json.loads(lp.read_text(encoding="utf-8"))
    cfg = Config.load(ROOT / "config.yaml")
    rows = []
    for model in FRAMING_MODELS:
        cfg.framing.model = model
        cfg.framing.input_size = 640
        base = gpu_mem_mb()
        t_load = time.time()
        imgs, ys, times, pe = [], [], [], 0.0
        for item in labels:
            p = ROOT / item["path"]
            im = imread(p)
            if im is None:
                continue
            imgs.append(im)
            ys.append(item)
        # 预热
        for im in imgs[:3]:
            classify(im, [], cfg)
        preds = []
        for im in imgs:
            t = time.perf_counter()
            r = classify(im, [], cfg)
            times.append((time.perf_counter() - t) * 1000)
            preds.append(r)
            pe = max(pe, gpu_mem_mb())
        valid = [(y, r) for y, r in zip(ys, preds) if y.get("keep") is not None and y["label"] != "unknown"]
        exact = sum(1 for y, r in valid if y["label"] == r["label"])
        keep = sum(1 for y, r in valid if bool(y["keep"]) == bool(r["keep"]))
        fb_truth = [(y, r) for y, r in valid if y["label"] == "full_body"]
        fb_recall = sum(1 for y, r in fb_truth if r["label"] == "full_body")
        pred_fb = [(y, r) for y, r in valid if r["label"] == "full_body"]
        false_fb = sum(1 for y, r in pred_fb if y["label"] != "full_body")
        ankle = sum(1 for y, r in fb_truth if r["visible"]["ankles"] >= 2)
        false_rej = sum(1 for y, r in valid if y["keep"] and not r["keep"])
        false_acc = sum(1 for y, r in valid if not y["keep"] and r["keep"])
        n_valid = max(1, len(valid))
        rows.append({
            "model": model, "n": len(ys), "n_valid": len(valid),
            "load_s": round(time.time() - t_load, 2),
            "exact_rate": round(exact / n_valid, 4),
            "keep_rate": round(keep / n_valid, 4),
            "fullbody_n": len(fb_truth),
            "fullbody_recall": round(fb_recall / max(1, len(fb_truth)), 4),
            "fullbody_precision": round((len(pred_fb) - false_fb) / max(1, len(pred_fb)), 4),
            "false_fullbody": false_fb,
            "ankle2_recall": round(ankle / max(1, len(fb_truth)), 4),
            "false_reject": false_rej, "false_accept": false_acc,
            "ms_mean": round(statistics.mean(times), 2),
            "ms_p95": round(_pct(times, 95), 2),
            "gpu_peak_mb": round(pe, 1), "gpu_delta_mb": round(pe - base, 1),
            "size_mb": round((ROOT / "models" / f"{model}.onnx").stat().st_size / 1024 / 1024, 1),
            "mismatch": [
                {"path": y["path"], "truth": y["label"], "pred": r["label"]}
                for y, r in valid if y["label"] != r["label"]
            ],
        })
        print(f"完成 {model}")
    report = {"kind": "framing", "rows": rows}
    _save(report, "framing")
    return report


def _save(report: dict, tag: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"model_bench_{tag}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {out}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "face"
    if mode == "face":
        face_suite()
    elif mode == "face-one":
        model, size, thresh = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
        print(json.dumps(face_one({"model": model, "det_size": size, "det_thresh": thresh}), ensure_ascii=False))
    elif mode == "far":
        far_suite()
    elif mode == "threshold":
        thr_suite()
    elif mode == "far-one":
        model, size, thresh = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
        print(json.dumps(face_far_one({"model": model, "det_size": size, "det_thresh": thresh}), ensure_ascii=False))
    elif mode == "framing":
        framing_suite(sys.argv[2] if len(sys.argv) > 2 else None)
    elif mode == "sheet":
        sheet()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
