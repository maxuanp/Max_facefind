from __future__ import annotations

from pathlib import Path

import yaml

from .config import Config

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _images_in_dir(path: Path) -> list[str]:
    return sorted(str(p.resolve()) for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def _as_paths(value: str | Path, base: Path | None = None) -> list[str]:
    p = Path(value)
    if not p.is_absolute() and base is not None:
        p = base / p
    if p.is_dir():
        return _images_in_dir(p)
    if p.is_file():
        return [str(p.resolve())]
    return []


def discover_references(cfg: Config, names: list[str] | None = None,
                        explicit: dict[str, list[str]] | None = None,
                        faces_dir: str | None = None) -> dict[str, list[str]]:
    """汇总参考照来源：

    1) CLI --face NAME=PATH（文件或目录，可多次）
    2) config.yaml people[].reference_images
    3) faces 目录下「子文件夹名 = 人名」
    4) faces 平铺目录 + 映射表（文件名 -> 人名）
    """
    base = Path(faces_dir).resolve() if faces_dir else cfg.faces_path
    refs: dict[str, list[str]] = {}

    def add(name: str, paths: list[str]) -> None:
        cur = refs.setdefault(name, [])
        for p in paths:
            if p not in cur:
                cur.append(p)

    for name, values in (explicit or {}).items():
        for value in values:
            add(name, _as_paths(value))

    for pc in cfg.people:
        for value in pc.reference_images:
            add(pc.name, _as_paths(value, cfg.root))

    if base.is_dir():
        for sub in sorted(base.iterdir()):
            if sub.is_dir():
                add(sub.name, _images_in_dir(sub))

        map_file = cfg.path(cfg.face.map_file)
        if map_file.exists():
            try:
                mapping = yaml.safe_load(map_file.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"参考照映射表解析失败：{map_file}（{exc}）") from exc
            for name, values in mapping.items():
                items = values if isinstance(values, list) else [values]
                for value in items:
                    p = Path(str(value))
                    if not p.is_absolute():
                        p = base / p
                    add(str(name), [str(p.resolve())] if p.is_file() else _images_in_dir(p))

    if names:
        wanted = list(dict.fromkeys(names))
        missing = [n for n in wanted if n not in refs]
        refs = {n: refs[n] for n in wanted if n in refs}
        for n in missing:
            refs.setdefault(n, [])
    return refs
