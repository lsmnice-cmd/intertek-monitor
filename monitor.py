# -*- coding: utf-8 -*-
"""
인터텍 코리아 공지 모니터 - GitHub Actions 버전 v1.1
[v1.1] 오류 내성: 연결 실패/Secrets 미등록 시 죽지 않고 원인을 로그에 출력
- 새 글 감지 → 상세 요약 → 텔레그램 / [모집완료] 감지
- 상태(seen.json)는 저장소에 커밋되어 유지됨 (워크플로가 처리)
- BOT_TOKEN / CHAT_ID 는 GitHub Secrets 환경변수에서 읽음
- 월~금 08~18시(한국시간)에만 확인 (그 외엔 즉시 종료)
"""

import os
import re
import sys
import json
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

BASE_URL = "https://www.intertek.co.kr"
TARGET_URL = "https://www.intertek.co.kr/common/crs/crs04-1.php"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "seen.json")

KST = timezone(timedelta(hours=9))

DATE_RE = re.compile(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
IDX_RE = re.compile(r"idx=(\d+)")
DONE_RE = re.compile(r"모집\s*완료|마감")

WORK_DAYS = (0, 1, 2, 3, 4)
WORK_START_HOUR = 8
WORK_END_HOUR = 18

BLOCK_KEYWORDS = ("captcha", "보안문자", "자동입력", "자동 입력",
                  "access denied", "비정상적인 접근", "cloudflare",
                  "validation request", "validation needed")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.intertek.co.kr/",
}


def log(msg):
    print(f"[{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_html(url):
    """GitHub 러너에서 requests로 읽기 → (html, blocked)"""
    import requests
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        log(f"❌ 연결 실패: {type(e).__name__}: {e}")
        log("   (인터텍이 해외 IP 접속을 막는 경우 GitHub 서버에서는 연결이 안 됩니다)")
        return None, True
    res.encoding = res.apparent_encoding
    html = res.text
    log(f"응답: HTTP {res.status_code}, {len(html)}자")
    if res.status_code in (403, 429, 503):
        return html, True
    return html, False


def in_work_hours(now=None):
    now = now or datetime.now(KST)
    return now.weekday() in WORK_DAYS and WORK_START_HOUR <= now.hour < WORK_END_HOUR

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.intertek.co.kr/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_session = None


def looks_blocked(status_code, html):
    """차단/보안문자 페이지 판별"""
    if status_code in (403, 429, 503):
        return True
    low = (html or "").lower()
    return any(k in low for k in BLOCK_KEYWORDS)


def parse_date(text):
    m = DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return datetime(y, int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def make_key(href, title):
    m = IDX_RE.search(href)
    if m:
        return f"idx-{m.group(1)}"
    if href and not href.lower().startswith("javascript"):
        return "h-" + hashlib.md5(href.encode("utf-8")).hexdigest()
    return "t-" + hashlib.md5(title.encode("utf-8")).hexdigest()


def parse_posts(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    posts = []
    for row in soup.select("table tr"):
        a = row.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 2:
            continue
        href = a["href"]
        link = urljoin(TARGET_URL, href) if not href.lower().startswith("javascript") else TARGET_URL
        posts.append({
            "key": make_key(href, title),
            "title": title,
            "link": link,
            "date": parse_date(row.get_text(" ", strip=True)),
        })

    if not posts:
        for a in soup.select("a[href]"):
            title = a.get_text(strip=True)
            href = a["href"]
            if not title or len(title) < 4:
                continue
            if any(x in href for x in ("#", "index", "main", "login")):
                continue
            link = urljoin(TARGET_URL, href) if not href.lower().startswith("javascript") else TARGET_URL
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else title
            posts.append({
                "key": make_key(href, title),
                "title": title,
                "link": link,
                "date": parse_date(parent_text),
            })
    return posts


def fetch_detail(link):
    """상세 페이지에서 참여시간 현황과 내용 요약 추출. 실패 시 None."""
    from bs4 import BeautifulSoup
    try:
        html, blocked = get_html(link)
        if blocked or html is None:
            raise RuntimeError("상세 페이지 차단/실패")
        soup = BeautifulSoup(html, "html.parser")

        detail = {"slots": [], "info": {}}
        for label in soup.select(".checkbox-tools_wrap label"):
            text = label.get_text(" ", strip=True)
            if text:
                detail["slots"].append(text)

        for tr in soup.select(".ctn table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            header = re.sub(r"\s+", "", tds[0].get_text(strip=True))
            lines = [p.get_text(" ", strip=True) for p in tds[1].find_all("p")]
            lines = [ln for ln in lines if ln]
            if not lines:
                lines = [tds[1].get_text(" ", strip=True)]
            detail["info"][header] = lines
        return detail
    except Exception as e:
        log(f"상세 페이지 파싱 실패({link}): {e}")
        return None


def build_message(prefix, post, detail):
    lines = [prefix, "", post["title"]]
    if post.get("date"):
        lines.append(f"게시일: {post['date']}")

    if detail:
        if detail["slots"]:
            lines.append("")
            lines.append("⏰ 참여시간 현황")
            for s in detail["slots"]:
                lines.append(f" · {s}")

        info = detail["info"]

        def add_section(icon, name, keys, max_lines=6):
            for k in keys:
                if k in info:
                    lines.append("")
                    lines.append(f"{icon} {name}")
                    for ln in info[k][:max_lines]:
                        lines.append(f" {ln}")
                    return

        add_section("📅", "시험 일정", ["시험일정"], max_lines=8)
        add_section("📝", "자격 요건", ["자격요건"], max_lines=3)
        add_section("⏱", "소요 시간", ["소요시간"], max_lines=2)
        add_section("🩹", "시험 부위", ["시험부위"], max_lines=2)
        add_section("💰", "교통비", ["교통비"], max_lines=2)
        add_section("📍", "위치", ["위치"], max_lines=2)

    lines.append("")
    lines.append(post["link"])

    msg = "\n".join(lines)
    if len(msg) > MAX_MSG_LEN:
        msg = msg[:MAX_MSG_LEN] + "\n…(생략)\n" + post["link"]
    return msg


def send_telegram(text):
    import requests
    if not BOT_TOKEN or not CHAT_ID:
        log("❌ 텔레그램 미발송: Secrets(BOT_TOKEN/CHAT_ID)가 등록되지 않았습니다")
        log("   저장소 Settings → Secrets and variables → Actions에서 등록하세요")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code != 200:
        log(f"❌ 텔레그램 발송 실패: HTTP {r.status_code} - {r.text[:200]}")
        log("   (401=봇토큰 오류, 400=채팅ID 오류/봇이 방에 없음)")
    r.raise_for_status()


def notify(prefix, post):
    try:
        detail = fetch_detail(post["link"])
        send_telegram(build_message(prefix, post, detail))
        log(f"알림 발송 [{prefix}]: {post['title']}")
    except Exception as e:
        log(f"⚠ 알림 발송 실패({post['title']}): {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_posts():
    html, hard_block = get_html(TARGET_URL)
    if hard_block or html is None:
        return [], True
    posts = parse_posts(html)
    if posts:
        return posts, False
    if looks_blocked(200, html):
        return [], True
    return [], False


def main():
    # v2: 월~금 08~18시에만 동작
    if not in_work_hours():
        log("근무시간 외 (월~금 08~18시만 확인) - 건너뜀")
        return

    state = load_state()
    was_blocked = bool(state.get("blocked"))

    posts, blocked = fetch_posts()

    if blocked:
        log("🚫 차단 감지 (보안문자/캡차 페이지)")
        if not was_blocked:
            # 차단 '시작' 시에만 1회 알림 (5분마다 도배 방지)
            try:
                send_telegram(
                    "🚫 인터텍 접속 차단 감지 (GitHub)\n\n"
                    "GitHub에서의 접속이 막혀 공지를 못 읽습니다.\n"
                    "GitHub 서버 IP도 차단된 상태입니다. 해제되면 다시 알려드릴게요.\n"
                    + TARGET_URL)
            except Exception:
                pass
            state["blocked"] = True
            save_state(state)
        return

    if not posts:
        log("게시글을 찾지 못했습니다. 페이지 구조 확인 필요.")
        return

    if was_blocked:
        # 차단 '해제' 시 1회 알림
        try:
            send_telegram("✅ 인터텍 접속 정상화\n\n차단이 풀려 공지 수집을 재개했습니다.")
        except Exception:
            pass
        state["blocked"] = False
        log("✅ 차단 해제 - 수집 재개")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    known = state.get("posts", {})

    if not known:
        for p in posts:
            if p.get("date") == today:
                notify("📢 인터텍 오늘 공지", p)
        known = {p["key"]: p["title"] for p in posts}
        log(f"기준점 등록 — {len(posts)}건 (오늘 글은 알림 발송)")
    else:
        changed = False
        for p in posts:
            old_title = known.get(p["key"])
            if old_title is None:
                notify("📢 인터텍 새 공지", p)
                known[p["key"]] = p["title"]
                changed = True
            elif old_title != p["title"]:
                if DONE_RE.search(p["title"]) and not DONE_RE.search(old_title):
                    notify("✅ 모집완료 되었습니다", p)
                known[p["key"]] = p["title"]
                changed = True
        if not changed:
            log("변경 없음")

    if len(known) > 500:
        known = dict(list(known.items())[-500:])

    state["posts"] = known
    state.pop("seen", None)
    save_state(state)



if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("오류 발생:\n" + traceback.format_exc())
        sys.exit(1)
