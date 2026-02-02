import os
import json
import hashlib
import smtplib
import datetime
import difflib
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URLS_FILE = Path("urls.txt")
STATE_FILE = Path("state.json")
STATE_TEXT_DIR = Path("state_text")  # 前回テキスト保存用

# ★改善：タイムアウトを延長（中小企業庁サイトなどが混むと30秒では落ちがち）
TIMEOUT = 60

UA = "Mozilla/5.0 (compatible; url-watch/1.0; +https://github.com/)"
DIFF_MAX_LINES = 20  # 最大20行（希望どおり）

def load_urls():
    urls = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls

def normalize_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)

# ★改善：一時的な失敗に強くする（1回だけリトライ）
def fetch(url: str) -> str:
    last_err = None
    for attempt in range(2):  # 合計2回（= 1回リトライ）
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(5)  # 5秒待って再試行
            else:
                raise last_err

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
    if p.exists():
        return p.read_text(encoding="utf-8", errors="ignore")
    return None

def save_curr_text(url: str, text: str):
    STATE_TEXT_DIR.mkdir(exist_ok=True)
    p = STATE_TEXT_DIR / f"{url_key(url)}.txt"
    p.write_text(text, encoding="utf-8")

def make_diff(prev_text: str, curr_text: str, max_lines: int = 20):
    prev_lines = prev_text.splitlines()
    curr_lines = curr_text.splitlines()

    diff_iter = difflib.unified_diff(
        prev_lines, curr_lines,
        fromfile="before", tofile="after",
        lineterm="", n=1
    )

    picked = []
    for ln in diff_iter:
        if ln.startswith(("---", "+++", "@@")):
            continue
        if ln.startswith(("+", "-")) and ln[1:].strip():
            picked.append(ln)
        if len(picked) >= max_lines:
            break
    return picked

def send_email(subject: str, body: str):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")  # 空でも587
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
    urls = load_urls()
    state = load_state()

    changed_reports = []
    errors = []

    for url in urls:
        try:
            html = fetch(url)
            curr_text = normalize_html_to_text(html)
            curr_hash = sha256(curr_text)

            prev_hash = state.get(url, {}).get("hash")
            prev_text = load_prev_text(url)

            if prev_hash and prev_hash != curr_hash and prev_text is not None:
                diff_lines = make_diff(prev_text, curr_text, DIFF_MAX_LINES)
                changed_reports.append((url, diff_lines))

            save_curr_text(url, curr_text)
            state[url] = {
                "hash": curr_hash,
                "last_checked": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
        except Exception as e:
            errors.append((url, str(e)))

    save_state(state)

    # 更新 or 取得エラーがあったときだけ通知（現状仕様のまま）
    if changed_reports or errors:
        today = datetime.date.today().isoformat()
        subject = f"[URL更新検知] {today} 変更:{len(changed_reports)} エラー:{len(errors)}"

        lines = []
        if changed_reports:
            lines.append("■ 更新が検知されたURL（差分 +追加 / -削除：最大20行）")
            lines.append("")
            for url, diff_lines in changed_reports:
                lines.append(f"【{url}】")
                if diff_lines:
                    lines.extend(diff_lines)
                else:
                    lines.append("(差分が大きい/特殊で20行以内に収まりませんでした)")
                lines.append("")

        if errors:
            lines.append("■ 取得エラー（サイト側ブロック/一時障害の可能性）")
            for u, err in errors:
                lines.append(f"- {u} : {err}")
            lines.append("")

        lines.append("（このメールは自動送信です）")
        send_email(subject, "\n".join(lines))
    else:
        print("No changes, no errors.")

if __name__ == "__main__":
    main()
