"""
Boshqaruv panelini ochadi.

    python -m tools.panel

Kassa ilovasisiz ishlaydi: faqat server ko'tariladi va brauzer ochiladi.
Do'kon egasi o'z kompyuterida panelni ochib, kassirlarni qo'shadi,
savdoni ko'radi.

Oyna yopilguncha server ishlab turadi. Yopilsa — panel ham yopiladi.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"        # brauzer shu manzilda ochiladi (shu kompyuter)
BIND_HOST = "0.0.0.0"     # boshqa kassalar ham ko'rishi uchun barcha tarmoqlar
PORT = 8000
BASE = f"http://{HOST}:{PORT}"


def env() -> dict:
    e = dict(os.environ)
    e.setdefault("SECRET_KEY", "local-sinov-uchun")
    e.setdefault("DEBUG", "True")
    e.setdefault("ALLOWED_HOSTS", "*")
    e.pop("DATABASE_URL", None)
    return e


def run(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "manage.py", *args], cwd=ROOT, env=env()
    )
    if result.returncode != 0:
        raise SystemExit(f"«{' '.join(args)}» bajarilmadi")


def wait_for_server(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/kirish/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    print("Baza tayyorlanmoqda...")
    run("migrate", "--no-input")
    run("setup_local")

    print("Server ishga tushmoqda...")
    server = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", f"{BIND_HOST}:{PORT}", "--noreload"],
        cwd=ROOT, env=env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        if not wait_for_server():
            print("Server ko'tarilmadi.")
            return 1

        print()
        print(f"  PANEL (shu kompyuter): {BASE}")
        print("  Kirish: admin / admin")
        print()
        print("  BOSHQA KASSALAR ulanishi uchun shu kompyuterning IP manzili:")
        print("    Win+R -> cmd -> ipconfig -> IPv4-адрес")
        print("    Kassada server manzili:  http://<IPv4>:8000")
        print()
        print("  Brauzer ochilmasa, yuqoridagi manzilni o'zingiz kiriting.")
        print()
        print("  Bu oynani YOPMANG — yopilsa panel ham yopiladi.")
        print()

        webbrowser.open(BASE)

        # Oyna yopilguncha kutamiz
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
