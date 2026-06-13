import json
import re
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
HAR = Path(__import__("os").environ.get("HAR_PATH", ROOT / "g21.tcsion.com3.har"))


def safe_value(name: str, value: str) -> str:
    lower = name.lower()
    if any(part in lower for part in ["pass", "pwd", "cookie", "token", "authorization"]):
        return "[redacted]"
    if len(value) > 180:
        return value[:180] + "..."
    return value


def main() -> None:
    data = json.loads(HAR.read_text(encoding="utf-8"))
    for index, entry in enumerate(data["log"]["entries"], start=1):
        request = entry["request"]
        response = entry["response"]
        parsed = urlparse(request["url"])
        text = (response.get("content") or {}).get("text", "")
        if "/Login/" not in parsed.path and "Login" not in text[:500]:
            continue
        print(f"\n#{index} {request['method']} {response['status']} {request['url']}")
        print(f"  response-url? {entry.get('pageref', '')}")
        for header in request.get("headers", []):
            name = header.get("name", "")
            if name.lower() in {"referer", "origin", "content-type", "location"}:
                print(f"  req.{name}: {header.get('value', '')}")
        post = request.get("postData") or {}
        if post:
            print(f"  post.mime={post.get('mimeType', '')}")
            if post.get("text"):
                redacted = re.sub(r"(?i)(password|pwd|token)=([^&]+)", r"\1=[redacted]", post["text"])
                print(f"  post.text={redacted[:500]}")
            for param in post.get("params", []):
                name = param.get("name", "")
                print(f"  post.{name}={safe_value(name, param.get('value', ''))}")
        for header in response.get("headers", []):
            name = header.get("name", "")
            if name.lower() in {"location", "content-type", "set-cookie"}:
                print(f"  res.{name}: {safe_value(name, header.get('value', ''))}")
        title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
        if title:
            print(f"  title={re.sub(r'\\s+', ' ', title.group(1)).strip()}")
        print(f"  body-start={text[:220].replace(chr(10), ' ')}")


if __name__ == "__main__":
    main()
