import json
import os
from pathlib import Path


path = Path(os.environ.get("HAR_PATH", "www.tcsion.com.har"))
data = json.loads(path.read_text(encoding="utf-8"))
for index, entry in enumerate(data["log"]["entries"], start=1):
    req = entry["request"]
    res = entry["response"]
    print(f"#{index} {req['method']} {req['url']} -> {res['status']}")
    print("request headers")
    for h in req.get("headers", []):
        name = h.get("name", "")
        value = h.get("value", "")
        if name.lower() in {"cookie", "authorization"}:
            value = "[redacted]"
        print(f"  {name}: {value}")
    post = req.get("postData") or {}
    if post:
        print("post")
        print(f"  mimeType={post.get('mimeType', '')}")
        print(f"  text={post.get('text', '')[:500]}")
        for param in post.get("params", []):
            print(f"  {param.get('name')}={param.get('value', '')[:200]}")
    print("response headers")
    for h in res.get("headers", []):
        name = h.get("name", "")
        value = h.get("value", "")
        if name.lower() == "set-cookie":
            value = "[redacted]"
        print(f"  {name}: {value}")
    content = res.get("content") or {}
    print(f"response text={content.get('text', '')[:500]}")
