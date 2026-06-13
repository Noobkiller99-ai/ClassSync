import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from class_sync.tcs import TcsClient, TcsError


def main() -> None:
    username = os.environ["TCSION_USER"]
    password = os.environ["TCSION_PASSWORD"]
    try:
        events = TcsClient().fetch_timetable(username, password)
    except TcsError as exc:
        print(f"status=failed")
        print(f"reason={exc}")
        return
    print("status=ok")
    print(f"events={len(events)}")
    for event in events[:5]:
        print(f"{event.starts_at.isoformat()} {event.title} {event.classroom}")


if __name__ == "__main__":
    main()
