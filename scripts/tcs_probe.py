import os
import re
from html import unescape
from urllib.parse import urljoin

import requests


LOGIN_URL = "https://www.tcsion.com/SelfServices/"


def inputs(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]+>", html, flags=re.I):
        name = attr(tag, "name")
        if not name:
            continue
        fields[name] = attr(tag, "value") or ""
    return fields


def attr(tag: str, name: str) -> str:
    match = re.search(rf"""{name}\s*=\s*(['"])(.*?)\1|{name}\s*=\s*([^\s>]+)""", tag, flags=re.I)
    if not match:
        return ""
    return unescape(match.group(2) or match.group(3) or "")


def main() -> None:
    username = os.environ["TCSION_USER"]
    password = os.environ["TCSION_PASSWORD"]
    session = requests.Session()
    session.headers.update({"User-Agent": "ClassSync/1.0"})
    entry = session.get(LOGIN_URL, timeout=30)
    entry.raise_for_status()
    form = re.search(r"<form[^>]+name=['\"]login['\"][\s\S]*?</form>", entry.text, flags=re.I)
    if not form:
        raise RuntimeError("Login form not found")
    form_html = form.group(0)
    action = attr(form_html, "action")
    base_data = inputs(form_html)
    print(f"entry={entry.status_code} {entry.url}")
    variants = [
        ("default", {}),
        ("unencrypted-flag", {"isPasswordEncrypted": "0"}),
        ("no-remember", {"remember": ""}),
        ("login-type-1", {"loginType": "1"}),
    ]
    for name, overrides in variants:
        session.cookies.clear()
        session.get(LOGIN_URL, timeout=30)
        data = dict(base_data)
        data.update({"accountname": username, "password": password, "remember": "on"})
        data.update(overrides)
        response = session.post(urljoin(entry.url, action), data=data, allow_redirects=True, timeout=30)
        print(f"\nvariant={name}")
        print(f"login={response.status_code} {response.url}")
        print(f"cookies={len(session.cookies)}")
        markers = ["SelfServices/home", "AttendancePeriodWiseServlet", "Invalid", "captcha", "OTP", "Password"]
        for marker in markers:
            print(f"contains:{marker}={marker.lower() in response.text.lower()}")
        print(f"title={title(response.text)}")
        reason = failure_reason(response.text)
        if reason:
            print(f"reason={reason}")


def title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip() if match else ""


def failure_reason(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", unescape(text))
    for marker in ["Invalid", "failed", "incorrect", "not registered"]:
        index = text.lower().find(marker.lower())
        if index >= 0:
            return text[max(0, index - 80) : index + 180].strip()
    return ""


if __name__ == "__main__":
    main()
