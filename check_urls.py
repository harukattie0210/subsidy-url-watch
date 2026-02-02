import os
import json
import hashlib
import smtplib
import datetime
import difflib
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URLS_FILE = Path("urls.txt")
STATE_FILE = Path("state.json")
STATE_TEXT_DIR = Path("state_text")

TIMEOUT = 30
UA = "Mozilla/5.0 (compatible; url-watch/1.0)"
DIFF_MAX_LINES = 20

def load_urls():
    return [
        line.strip()
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

def normalize_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    return "\n".join(lines)

def fetch(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_prev_text(url: str):
    p = STATE_TEXT_DIR / f"{url_key(url)}.txt"
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None

def save_curr_text(url: str, text: str):
    STATE_TEXT_DIR.mkdir(exist_ok=True)
    (STATE_TEXT_DIR / f"{url_key(url)}.txt").write_text(text, encoding="utf-8")

def make_diff(prev: str, curr: str):
    diff = difflib.unified_diff(prev.splitlines(), curr.splitlines(), lineterm="")
    lines = []
    for ln in diff:
        if ln.startswith(("---", "+++", "@@")):
            continue
        if ln.startswith(("+", "-")) and ln[1:].strip():
            lines.append(ln)
        if len(lines) >= DIFF_MAX_LINES:
            break
    return lines

def send_email(subject: str, body: str):
    smtp_host = os.environ["SMTP_HOST"]
    # SMTP_PORT が未設定/空でも 587 を使う
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(mail_from, [mail_to], msg.as_string())


def main():
    state = load_state()
    reports = []
    errors = []

    for url in load_urls():
        try:
            text = normalize_html_to_text(fetch(url))
            h = sha256(text)
            prev_h = state.get(url, {}).get("hash")
            prev_text = load_prev_text(url)

            if prev_h and prev_h != h and prev_text:
                reports.append((url, make_diff(prev_text, text)))

            save_curr_text(url, text)
            state[url] = {"hash": h}

        except Exception as e:
            errors.append(f"{url} : {e}")

    save_state(state)

    if reports or errors:
        body = []
        for url, diff in reports:
            body.append(f"【{url}】")
            body.extend(diff or ["(差分が大きすぎます)"])
            body.append("")
        if errors:
            body.append("■ エラー")
            body.extend(errors)

        send_email(
            f"[URL更新検知] {datetime.date.today()}",
            "\n".join(body),
        )

if __name__ == "__main__":
    main()
