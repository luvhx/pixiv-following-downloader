"""Docker / 反向代理健康检查：请求 WebUI 的 /healthz。"""
import os
import sys
import urllib.request


def main() -> int:
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    port = os.environ.get("WEBUI_PORT", "8971")
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
