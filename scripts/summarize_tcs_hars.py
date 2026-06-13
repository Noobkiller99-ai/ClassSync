import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
FILES = ["www.tcsion.com.har", "g21.tcsion.com2.har", "g21.tcsion.com1.har", "g21.tcsion.com.har"]


def main() -> None:
    for name in FILES:
        path = ROOT / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n{name}")
        print(f"entries={len(data['log']['entries'])}")
        for entry in data["log"]["entries"]:
            req = entry["request"]
            res = entry["response"]
            parsed = urlparse(req["url"])
            print(f"{req['method']:4} {res['status']} {parsed.netloc}{parsed.path}")
            params = {}
            if parsed.query:
                params.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
            for param in (req.get("postData") or {}).get("params", []):
                params[param.get("name", "")] = param.get("value", "")
            interesting = {
                key: value
                for key, value in params.items()
                if key.lower()
                in {
                    "subaction",
                    "widgetsolutionid",
                    "appid",
                    "launchkey",
                    "urn",
                    "subaction",
                    "templateid",
                    "orgcatid",
                    "compid",
                    "targetsolutionid",
                    "userid",
                    "userid",
                    "orgid",
                    "permissionid",
                    "entitytypeid",
                    "formid",
                    "reference_id",
                    "operation",
                    "methodname",
                    "classname",
                    "usrid",
                    "dcnid",
                }
            }
            if interesting:
                print("     " + ", ".join(f"{k}={v}" for k, v in interesting.items()))
            text = (res.get("content") or {}).get("text", "")
            launch = re.search(r"<launchkeyID>(.*?)</launchkeyID>", text)
            if launch:
                print(f"     response.launchkeyID={launch.group(1)}")
            if parsed.path.endswith("AttendancePeriodWiseServlet") and text:
                print(f"     response.starts={text[:90]}")


if __name__ == "__main__":
    main()
