"""Read-only check that Railway serves the expected release and a working DB."""
import json
import os
import time
import urllib.request
expected = os.environ.get("GITHUB_SHA", "")
url = "https://hub-production-0882.up.railway.app/health/"
for attempt in range(40):
    try:
        request = urllib.request.Request(url, headers={"Cache-Control":"no-cache"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        if data.get("status") == "ok" and data.get("release") == "sevimli-refresh-20260912":
            revision = data.get("revision", "")
            if revision and revision != expected:
                print("Waiting for this commit to deploy", flush=True)
            else:
                print("Railway health and release verified; revision:", revision or "not exposed")
                break
        else:
            print("Waiting for new release", flush=True)
    except Exception as exc:
        print("Health check pending:", type(exc).__name__, flush=True)
    time.sleep(15)
else:
    raise SystemExit("Railway deployment did not become healthy in time")
