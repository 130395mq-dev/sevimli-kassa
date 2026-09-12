"""Render isolated demo pages in CI; never connect to production."""
import os
if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("MOYSKLAD_TOKEN"):
    raise SystemExit("This preview runs only in isolated CI")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import base64
import io
import threading
import django
django.setup()
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.contrib.auth.models import User
from django.test import Client
from wsgiref.simple_server import make_server
from playwright.sync_api import sync_playwright

call_command("migrate", interactive=False, stdout=io.StringIO())
call_command("setup_local", with_sales=True, stdout=io.StringIO())
client = Client()
client.force_login(User.objects.get(username="admin"))
server = make_server("127.0.0.1", 8765, StaticFilesHandler(get_wsgi_application()))
threading.Thread(target=server.serve_forever, daemon=True).start()
out = Path("previews")
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width":1440,"height":1000}, device_scale_factor=1)
    context.add_cookies([{"name":"sessionid", "value":client.cookies["sessionid"].value,
                         "domain":"127.0.0.1","path":"/"}])
    page = context.new_page()
    for name, path in [("dashboard","/"), ("registers","/kassalar/"), ("admin","/admin/"),
                       ("admin-payments","/admin/sales/paymentmethod/")]:
        response = page.goto("http://127.0.0.1:8765" + path)
        assert response.status == 200, (path, response.status)
        page.screenshot(path=str(out / (name + ".png")))
    page.set_viewport_size({"width":390,"height":844})
    page.goto("http://127.0.0.1:8765/")
    page.screenshot(path=str(out / "dashboard-mobile.png"))
    browser.close()
server.shutdown()
for name in ("dashboard.png", "admin.png", "dashboard-mobile.png"):
    print("PREVIEW_PNG:" + name + ":" + base64.b64encode((out / name).read_bytes()).decode())
print("Dashboard and Django admin screenshots rendered")
