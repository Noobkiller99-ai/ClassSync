import json
import os
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
HAR = Path(os.environ.get("HAR_PATH", ROOT / "g21.tcsion.com.har"))


def main() -> None:
    data = json.loads(HAR.read_text(encoding="utf-8"))
    entries = data["log"]["entries"]
    print(f"entries={len(entries)}")
    seen = set()
    for entry in entries:
        request = entry["request"]
        response = entry["response"]
        url = request["url"]
        parsed = urlparse(url)
        key = (request["method"], parsed.path, response.get("status"))
        if key in seen:
            continue
        seen.add(key)
        text = ""
        content = response.get("content") or {}
        if content.get("text"):
            text = content["text"][:120].replace("\n", " ")
        print(f"{request['method']:4} {response.get('status')} {parsed.path} {text}")

    print("\ninteresting entries")
    needles = ["Time :", "Faculty", "Room No", "Attendance", "Session", "Classroom"]
    for entry in entries:
        request = entry["request"]
        response = entry["response"]
        url = request["url"]
        parsed = urlparse(url)
        content = response.get("content") or {}
        text = content.get("text") or ""
        if not any(needle in text for needle in needles):
            continue
        print(f"\n{request['method']} {parsed.path} status={response.get('status')}")
        params = request.get("postData", {}).get("params", [])
        for param in params[:30]:
            value = param.get("value", "")
            print(f"  {param.get('name')}={value[:90]}")
        if parsed.path.endswith("/cms/AttendancePeriodWiseServlet"):
            sample = ROOT / "scripts" / "attendance_sample.json"
            sample.write_text(text, encoding="utf-8")
            print(f"  wrote {sample.relative_to(ROOT)}")
        for needle in needles:
            index = text.find(needle)
            if index >= 0:
                start = max(0, index - 180)
                end = min(len(text), index + 320)
                print(text[start:end].replace("\n", " ")[:700])
                break

    print("\npost payloads")
    for entry in entries:
        request = entry["request"]
        if request["method"] != "POST":
            continue
        parsed = urlparse(request["url"])
        print(f"\nPOST {parsed.path}")
        post_data = request.get("postData") or {}
        for param in post_data.get("params", [])[:60]:
            name = param.get("name", "")
            value = param.get("value", "")
            if any(secret in name.lower() for secret in ["pass", "pwd", "token", "session"]):
                value = "[redacted]"
            print(f"  {name}={value[:140]}")


if __name__ == "__main__":
    main()
