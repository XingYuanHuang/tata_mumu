# -*- coding: utf-8 -*-
"""在有代理限制的机器上：绕过 pip 的 UA 拦截，直接抓 wheel 到本地目录。

用法: python _pipfetch.py 包名 [包名 ...]
"""
import re
import sys
import urllib.request
from pathlib import Path

from pip._vendor.packaging.version import InvalidVersion, Version

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple/"
OUT = Path(__file__).parent / "_wheels"
OUT.mkdir(exist_ok=True)

BAD_OS = ("macosx", "manylinux", "musllinux", "win32", "arm64", "aarch64", "i686")


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def fetch(url):
    return urllib.request.urlopen(url, timeout=60).read()


def compat(filename):
    """挑 py3.13 / win_amd64 能装的轮子"""
    low = filename.lower()
    if not low.endswith(".whl") or any(b in low for b in BAD_OS):
        return False
    if "win_amd64" not in low and "none-any" not in low:
        return False
    if "cp313t" in low or "cp312" in low or "cp311" in low or "cp310" in low:
        return False
    if "cp3" in low and "cp313" not in low and "abi3" not in low and "none-any" not in low:
        return False
    m = re.findall(r"cp3(\d+)", low)
    if m and min(int(x) for x in m) > 13:
        return False
    return True


def pick(pkg):
    page = MIRROR + norm(pkg) + "/"
    html = fetch(page).decode("utf-8", "ignore")
    best = None
    for href in re.findall(r'href="([^"#]+)', html):
        fn = href.rsplit("/", 1)[-1]
        if not compat(fn):
            continue
        parts = fn.split("-")
        ver = parts[1] if len(parts) >= 5 else ""
        try:
            v = Version(ver)
        except InvalidVersion:
            continue
        key = (not v.is_prerelease, v)
        if best is None or key > best[0]:
            best = (key, href, fn, v)
    if not best:
        return None
    _, href, fn, v = best
    dest = OUT / fn
    if not dest.exists():
        data = fetch(urllib.request.urljoin(page, href))
        dest.write_bytes(data)
        print(f"  + {fn} ({len(data)//1024} KB)")
    else:
        print(f"  = {fn} (已存在)")
    return fn


def main(names):
    for n in names:
        try:
            if not pick(n):
                print(f"  ! {n}: 无可用轮子")
        except Exception as e:
            print(f"  ! {n}: {type(e).__name__} {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
