import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from class_sync.tcs import TcsClient, TcsError


def show(label: str, text: str) -> None:
    compact = " ".join(text.strip().split())
    print(f"{label}: {compact[:300]}")


def main() -> None:
    client = TcsClient()
    try:
        client.login(os.environ["TCSION_USER"], os.environ["TCSION_PASSWORD"])
        print(f"login=ok base={client.base_url}")
        client.bootstrap_dashboard()
        print("dashboard=ok")
        client.open_timetable_app()
        print("timetable_app=ok")
        for label, data in [
            (
                "sync",
                {
                    "REFERENCE_ID": "cms_03621",
                    "orgId": "2782",
                    "permissionId": "106429",
                    "entityTypeId": "101762",
                    "userId": client.user_id(),
                    "appid": "9520",
                    "operation": "syncmessages",
                    "formId": "101782",
                },
            ),
            ("false", {"REFERENCE_ID": "cms_05340", "permissionId": "100733", "entityTypeId": "100138"}),
            (
                "sgm",
                {
                    "className": "com.tcs.cmstimetable.action.timetable.StudentTimetable.StudentTimetableNew",
                    "methodName": "getAllSGMs",
                    "permissionId": "106554",
                    "entityTypeId": "101782",
                    "userID": client.user_id(),
                },
            ),
            ("final", {"REFERENCE_ID": "cms_03954", "permissionId": "106554", "entityTypeId": "101782"}),
        ]:
            response = client.post_attendance(data)
            print(f"{label}=http{response.status_code} content-type={response.headers.get('content-type')}")
            show(label, response.text)
    except TcsError as exc:
        print(f"failed={exc}")


if __name__ == "__main__":
    main()
