"""Assemble official upstream binaries locally; never republish them in MinMin ZIPs."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

KINDS = {"windows-x64": {"concat-msi", "ffmpeg-zip"},
         "linux-x64": {"concat-deb", "ffmpeg-tar"}}
NATIVE = {"windows-x64": {"@ffmpeg-installer/win32-x64", "@ffprobe-installer/win32-x64", "@remotion/compositor-win32-x64-msvc"},
          "linux-x64": {"@ffmpeg-installer/linux-x64", "@ffprobe-installer/linux-x64", "@remotion/compositor-linux-x64-gnu", "@remotion/compositor-linux-x64-musl"}}


def _relative(text):
    p = PurePosixPath(text)
    if not text or p.is_absolute() or ".." in p.parts or ":" in text or "\\" in text:
        raise ValueError("Đường dẫn upstream không hợp lệ")
    return p


def validate(assets, target):
    if not isinstance(assets, list) or not 2 <= len(assets) <= 8:
        raise ValueError("Thiếu gói Concat/FFmpeg chính chủ")
    if {a.get("kind") for a in assets if a.get("kind") not in {"npm-native", "browser-zip"}} != KINDS.get(target, set()):
        raise ValueError("Gói công cụ không đúng hệ điều hành")
    seen = set()
    for a in assets:
        p = urlparse(a["url"])
        if a["kind"] == "npm-native":
            package, version = a.get("package", ""), a.get("version", "")
            if package not in NATIVE.get(target, set()) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("Gói native npm không hợp lệ")
            expected = f"https://registry.npmjs.org/{package}/-/{package.split('/')[-1]}-{version}.tgz"
            valid = a["url"] == expected
            identity = package
        elif a["kind"] == "browser-zip":
            host_target = "win64" if target == "windows-x64" else "linux64"
            valid = bool(re.fullmatch(rf"https://cdn\.playwright\.dev/builds/cft/[0-9.]+/{host_target}/chrome-headless-shell-{host_target}\.zip", a["url"]))
            valid = valid and bool(re.fullmatch(r"chromium_headless_shell-[0-9]+", a.get("directory", "")))
            identity = "browser"
        else:
            prefix = "/jub0t/Concat/releases/download/" if a["kind"].startswith("concat-") else "/BtbN/FFmpeg-Builds/releases/download/"
            valid = p.hostname == "github.com" and p.path.startswith(prefix)
            identity = a["kind"]
        if identity in seen:
            raise ValueError("Gói upstream bị lặp")
        seen.add(identity)
        if not valid or p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.query or p.fragment:
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
        if kind == "npm-native":
            dest = root / "node_modules" / a["package"]
            with tarfile.open(path, "r:gz") as t:
                total = 0
                for item in t.getmembers():
                    relative = _relative(item.name)
                    if relative.parts[0] != "package" or item.issym() or item.islnk() or not (item.isfile() or item.isdir()):
                        raise ValueError("Npm archive không hợp lệ")
                    total += item.size
                    if total > 512 * 1024**2:
                        raise ValueError("Npm archive quá lớn")
                    if item.isdir(): continue
                    output = dest.joinpath(*relative.parts[1:])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with t.extractfile(item) as src, output.open("wb") as out: shutil.copyfileobj(src,out)
                    if os.name != "nt": output.chmod(0o755 if item.mode & 0o111 else 0o644)
        elif kind == "browser-zip":
            dest = root / "browsers" / a["directory"]
            with zipfile.ZipFile(path) as z:
                total = 0
                for item in z.infolist():
                    relative = _relative(item.filename)
                    if stat.S_ISLNK(item.external_attr >> 16): raise ValueError("Browser archive chứa link")
                    total += item.file_size
                    if total > 1024**3: raise ValueError("Browser archive quá lớn")
                    if item.is_dir(): continue
                    output = dest.joinpath(*relative.parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(item) as src, output.open("wb") as out: shutil.copyfileobj(src,out)
                    if os.name != "nt": output.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
            (dest / "INSTALLATION_COMPLETE").touch()
        elif kind == "concat-msi":
            dest = root / "concat"
            dest.mkdir(exist_ok=True)
            msi = path.with_suffix(".msi")
            path.rename(msi)
            executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/msiexec.exe"
            subprocess.run([str(executable), "/a", str(msi), "/qn", "TARGETDIR=" + str(dest)],
                           check=True, timeout=180, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for administrative_copy in dest.rglob("*.msi"):
                if administrative_copy.is_file() and administrative_copy.resolve().is_relative_to(dest.resolve()):
                    administrative_copy.unlink()
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
