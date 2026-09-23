"""Small real-HTTP check against a running local demo; does not change skills/history."""

import argparse
import http.cookiejar
import json
import os
import re
import statistics
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if urllib.parse.urlparse(base).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("This smoke script targets local demo instances only.")
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path, payload=None, as_json=False):
        headers = {"Referer": base + "/"}
        data = None
        if payload is not None:
            token = next((c.value for c in jar if c.name == "csrftoken"), "")
            headers["X-CSRFToken"] = token
            headers["Content-Type"] = "application/json" if as_json else "application/x-www-form-urlencoded"
            data = (json.dumps(payload) if as_json else urllib.parse.urlencode(payload)).encode()
        with client.open(
            urllib.request.Request(base + path, data=data, headers=headers), timeout=15
        ) as response:
            return response.read().decode()

    def login(username):
        html = call("/login/")
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1)
        html = call(
            "/login/",
            {
                "username": username,
                "password": os.getenv("DEMO_PASSWORD", "career-demo-2026"),
                "csrfmiddlewaretoken": token,
            },
        )
        assert "Выйти" in html, "Login failed"

    output = {}
    login("employee")
    for label, path, payload in [
        ("profile_html", "/people/E0028/", None),
        ("profile_api", "/api/people/E0028/", None),
        ("recommendations", "/api/people/E0028/recommendations/", {}),
    ]:
        durations = []
        for _ in range(5):
            start = time.perf_counter()
            body = call(path, payload, as_json=payload is not None)
            durations.append(round((time.perf_counter() - start) * 1000, 1))
        output[label] = {"requests": 5, "median_ms": statistics.median(durations), "max_ms": max(durations)}
        if payload is not None:
            result = json.loads(body)
            output[label].update(mode=result["mode"], last_cached=result["cached"])
    call("/logout/", {})
    login("hr")
    durations = []
    for _ in range(5):
        start = time.perf_counter()
        assert "Профили сотрудников" in call("/hr/")
        durations.append(round((time.perf_counter() - start) * 1000, 1))
    output["hr_html"] = {"requests": 5, "median_ms": statistics.median(durations), "max_ms": max(durations)}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
