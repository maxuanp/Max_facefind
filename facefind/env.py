from __future__ import annotations

import glob
import os
import platform
import shutil
import sys
from dataclasses import dataclass

_PREPARED = False


def _cuda_toolkit_bins() -> list[str]:
    """onnxruntime-gpu 1.21 需要 CUDA 12.x；优先选版本号最高的 v12。"""
    bases = [
        os.environ.get("CUDA_PATH", ""),
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA",
    ]
    out: list[str] = []
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        versions = [d for d in os.listdir(base) if d.lower().startswith("v12.")]
        versions.sort(key=lambda s: [int(x) for x in s[1:].split(".") if x.isdigit()], reverse=True)
        for v in versions:
            for sub in ("bin", os.path.join("extras", "CUPTI", "lib64")):
                p = os.path.join(base, v, sub)
                if os.path.isdir(p):
                    out.append(p)
    return out


def _nvidia_site_bins() -> list[str]:
    try:
        import site

        roots = [os.path.join(p, "nvidia") for p in site.getsitepackages()]
    except Exception:
        return []
    out: list[str] = []
    for root in roots:
        if os.path.isdir(root):
            out.extend(glob.glob(os.path.join(root, "*", "bin")))
    return out


def prepare_cuda_dlls() -> list[str]:
    """把 CUDA / cuDNN 的 DLL 目录前置到 PATH，使 onnxruntime CUDA provider 能加载。

    返回实际加入的目录列表。重复调用无副作用。
    """
    global _PREPARED
    if _PREPARED:
        return []
    _PREPARED = True
    added: list[str] = []
    for d in _cuda_toolkit_bins() + _nvidia_site_bins():
        if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            added.append(d)
    return added


@dataclass
class GpuStatus:
    torch_version: str
    torch_cuda: bool
    torch_device: str
    ort_version: str
    ort_providers: list[str]
    gpu_usable: bool


def gpu_status() -> GpuStatus:
    prepare_cuda_dlls()
    torch_version = torch_cuda = torch_device = ""
    try:
        import torch

        torch_version = torch.__version__
        torch_cuda = bool(torch.cuda.is_available())
        if torch_cuda:
            torch_device = torch.cuda.get_device_name(0)
    except Exception as exc:  # torch 可能未安装
        torch_version = f"unavailable ({exc.__class__.__name__})"
    try:
        import onnxruntime as ort

        ort_version = ort.__version__
        providers = ort.get_available_providers()
    except Exception as exc:
        ort_version = f"unavailable ({exc.__class__.__name__})"
        providers = []
    return GpuStatus(
        torch_version=torch_version,
        torch_cuda=torch_cuda,
        torch_device=torch_device,
        ort_version=ort_version,
        ort_providers=providers,
        gpu_usable="CUDAExecutionProvider" in providers,
    )


def browser_status() -> dict[str, str]:
    """探测本机可用浏览器与 Playwright/Selenium 状态。"""
    candidates = {
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
        "edge": [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ],
    }
    out: dict[str, str] = {}
    for name, paths in candidates.items():
        found = next((p for p in paths if os.path.exists(p)), "")
        out[name] = found or "not found"
    for mod in ("playwright", "selenium"):
        try:
            __import__(mod)
            out[mod] = "installed"
        except Exception:
            out[mod] = "not installed"
    out["ffmpeg"] = shutil.which("ffmpeg") or "not found"
    return out


def python_status() -> dict[str, str]:
    return {
        "python": f"{sys.version.split()[0]} ({sys.executable})",
        "platform": f"{platform.system()} {platform.release()}",
    }
