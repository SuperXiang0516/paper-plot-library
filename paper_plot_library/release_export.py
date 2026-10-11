"""Whitelist-only publication artifact, separate from user-editable local preview."""
from pathlib import Path
import re
import shutil
import tempfile

from .site import build_site
from .storage import read_json, write_json


def export_site(root):
    root = Path(root)
    source = build_site(root, read_json(root / "data" / "catalog.json"), read_json(root / "data" / "updates.json", []))
    private = root / ".ppl"
    if private.is_symlink():
        raise ValueError("不通过符号链接创建发布产物")
    private.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="publish-", dir=private))
    fixed = ["index.html", "app.js", "styles.css", "data/catalog.json", "data/updates.json", "data/assets-manifest.json"]
    assets = read_json(source / "data" / "assets-manifest.json", [])
    if not isinstance(assets, list):
        raise ValueError("发布图片清单无效")
    for relative in fixed + assets:
        if relative not in fixed and (not isinstance(relative, str) or not re.fullmatch(r"assets/[\w./-]+\.(?:png|jpg|jpeg|gif|webp)", relative) or any(p in {".", ".."} for p in relative.split("/"))):
            raise ValueError("发布文件路径不安全")
        candidate = source / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("发布文件缺失或为符号链接")
        candidate.resolve().relative_to(source.resolve())
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, target)
    # Fixed pointer does not replace or delete a prior user's directory.
    write_json(private / "last-publication.json", {"path": str(output)})
    return output
