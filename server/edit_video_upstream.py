"""Assemble official upstream binaries locally; never republish them in MinMin ZIPs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

KINDS = {"windows-x64": {"concat-msi", "ffmpeg-zip"},
         "linux-x64": {"concat-deb", "ffmpeg-tar"}}


def validate(assets, target):
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("Thiếu gói Concat/FFmpeg chính chủ")
    if {a.get("kind") for a in assets} != KINDS.get(target, set()):
        raise ValueError("Gói công cụ không đúng hệ điều hành")
    for a in assets:
        p = urlparse(a["url"])
        prefix = "/jub0t/Concat/releases/download/" if a["kind"].startswith("concat-") else "/BtbN/FFmpeg-Builds/releases/download/"
        if p.scheme != "https" or p.hostname != "github.com" or p.username or p.password or p.port not in (None, 443) or not p.path.startswith(prefix) or p.query or p.fragment:
            raise ValueError("Công cụ phải tải trực tiếp từ repo chính chủ")
        if not re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256", ""))) or not 0 < int(a.get("size", 0)) <= 1024**3:
            raise ValueError("Thiếu dấu vân tay/dung lượng công cụ")
    return assets


def assemble(root, assets, target, download, cache):
    root, cache = Path(root), Path(cache)
    validate(assets, target)
    cache.mkdir(parents=True, exist_ok=True)
    for index, a in enumerate(assets):
        path = cache / (str(index) + ".download")
        received = download(a["url"], path, a["size"], a["sha256"], upstream=True)
        if received != a["size"]:
            raise ValueError("Dung lượng công cụ không khớp")
        kind = a["kind"]
        if kind == "concat-msi":
            dest = root / "concat"
            dest.mkdir(exist_ok=True)
            msi = path.with_suffix(".msi")
            path.rename(msi)
            executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/msiexec.exe"
            subprocess.run([str(executable), "/a", str(msi), "/qn", "TARGETDIR=" + str(dest)],
                           check=True, timeout=180, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif kind == "concat-deb":
            executable = shutil.which("dpkg-deb")
            if not executable:
                raise ValueError("Linux cần dpkg-deb để giải gói Concat (có sẵn trong bản Docker MinMin)")
            subprocess.run([executable, "-x", str(path), str(root / "concat")],
                           check=True, timeout=180, capture_output=True)
        else:
            dest = root / "bin"
            dest.mkdir(exist_ok=True)
            notices = root / "notices"
            notices.mkdir(exist_ok=True)
            if kind == "ffmpeg-zip":
                with zipfile.ZipFile(path) as z:
                    for name in ("ffmpeg", "ffprobe"):
                        matches = [i for i in z.infolist() if i.filename.endswith("/bin/" + name + ".exe")]
                        if len(matches) != 1 or matches[0].file_size > 512 * 1024**2:
                            raise ValueError("Gói FFmpeg không hợp lệ")
                        with z.open(matches[0]) as src, (dest / (name + ".exe")).open("wb") as out:
                            shutil.copyfileobj(src, out)
                    for item in z.infolist():
                        if not item.is_dir() and item.file_size <= 1024**2 and any(s in item.filename.lower() for s in ("license", "copying")):
                            (notices / ("FFMPEG-" + Path(item.filename).name)).write_bytes(z.read(item))
            else:
                with tarfile.open(path) as t:
                    for name in ("ffmpeg", "ffprobe"):
                        matches = [i for i in t.getmembers() if i.name.endswith("/bin/" + name)]
                        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 512 * 1024**2:
                            raise ValueError("Gói FFmpeg không hợp lệ")
                        with t.extractfile(matches[0]) as src, (dest / name).open("wb") as out:
                            shutil.copyfileobj(src, out)
                        (dest / name).chmod(0o755)
                    for item in t.getmembers():
                        if item.isfile() and item.size <= 1024**2 and any(s in item.name.lower() for s in ("license", "copying")):
                            (notices / ("FFMPEG-" + Path(item.name).name)).write_bytes(t.extractfile(item).read())
