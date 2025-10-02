import os, re, ssl, smtplib, json, random, time, traceback
from pathlib import Path
from email.mime.text import MIMEText
import yaml

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://web.loop.allocate-cloud.co.uk"
START_URL = f"{BASE_URL}/loop"

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "storage_state.json"     # Playwright session (persisted to repo)
SEEN_FILE  = ROOT / "seen_ids.json"          # Last-seen Request IDs (persisted to repo)
RULES_FILE = ROOT / "rules.yaml"

USER_AGENTS = [
    # keep a few realistic UAs; Playwright already does a lot, this just adds variation
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def jitter_sleep():
    # Shorter, still human-like jitter: 4–37 seconds
    import random, time
    delay = random.uniform(4, 37)
    print(f"jitter: sleeping {delay:.1f}s before scrape")
    time.sleep(delay)


def micro_pause():
    time.sleep(random.uniform(0.3, 1.1))

def load_rules():
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("rules", [])

def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(ids):
    SEEN_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False))

def fmt_ul(rows):
    lis = "".join(
        f"<li><b>{r['date']}</b> — {r['start_end']} — {r['unit']} ({r['grade']}) "
        f"[ID {r['request_id']}]</li>"
        for r in rows
    )
    return f"<ul>{lis}</ul>"

def send_email(subject, html):
    msg = MIMEText(html, "html")
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = os.environ["SMTP_TO"]
    msg["Subject"] = subject
    ctx = ssl.create_default_context()
    port = int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.sendmail(msg["From"], [msg["To"]], msg.as_string())

def new_context(p):
    ua = random.choice(USER_AGENTS)
    vp = {
        "width": random.choice([1280, 1366, 1440, 1536]),
        "height": random.choice([760, 800, 864, 900])
    }
    browser = p.chromium.launch(headless=True)
    args = {"user_agent": ua, "viewport": vp, "locale": "en-GB"}
    if STATE_FILE.exists():
        context = browser.new_context(storage_state=str(STATE_FILE), **args)
    else:
        context = browser.new_context(**args)
    context.set_extra_http_headers({"Accept-Language": "en-GB,en;q=0.9"})
    return browser, context

class AuthError(Exception):
    pass


class CaptchaError(Exception):
    pass


def detect_captcha(page):
    selectors = [
        "iframe[src*='captcha' i]",
        "[class*='captcha' i]",
        "text=/i am not a robot/i",
        "text=/captcha/i",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def needs_login(page):
    try:
        if detect_captcha(page):
            raise CaptchaError("CAPTCHA encountered")
    except CaptchaError:
        raise
    except Exception:
        pass
    url = (page.url or "").lower()
    if "login" in url:
        return True
    try:
        if page.locator("input[type='password']").count() > 0:
            return True
    except Exception:
        pass
    try:
        if page.locator("text=/welcome to loop/i").count() > 0:
            return True
    except Exception:
        pass
    return False


def perform_login(page):
    if detect_captcha(page):
        raise CaptchaError("CAPTCHA encountered during login")
    user = os.environ["ALLOCATE_USER"]
    pw = os.environ["ALLOCATE_PASS"]

    try:
        welcome_text = page.get_by_text(re.compile("welcome to loop", re.I))
        welcome_login_btn = page.get_by_role("button", name=re.compile("log.?in", re.I))
        if (
            welcome_text.count() > 0
            and welcome_login_btn.count() > 0
            and welcome_login_btn.first.is_visible()
        ):
            print("at welcome card")
            welcome_login_btn.first.click()
            print("clicked Log In")
            page.wait_for_load_state("networkidle", timeout=60000)
            micro_pause()
    except Exception:
        pass

    auth0_container = None
    try:
        page.wait_for_selector(
            ".auth0-lock-form, .auth0-lock-cred-pane-internal-wrapper",
            state="visible",
            timeout=60000,
        )
        container_candidates = page.locator(
            ".auth0-lock-form, .auth0-lock-cred-pane-internal-wrapper"
        )
        if container_candidates.count() > 0:
            auth0_container = container_candidates.first
            print("login: at auth0 form")
        micro_pause()
    except PWTimeout:
        raise AuthError("Auth0 Lock form did not appear")
    except Exception:
        pass

    def pick_locator(kind, value):
        locators = []
        if kind == "css":
            if auth0_container is not None:
                locators.append(auth0_container.locator(value))
            locators.append(page.locator(value))
        elif kind == "placeholder":
            if auth0_container is not None:
                try:
                    locators.append(auth0_container.get_by_placeholder(value))
                except Exception:
                    pass
            locators.append(page.get_by_placeholder(value))
        else:
            return None
        for loc in locators:
            try:
                if loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    email_candidates = [
        ("css", ".auth0-lock-input-email input"),
        ("css", "input[name='email']"),
        ("css", "input#1-email"),
        ("css", "input[type='email']"),
        ("placeholder", "yours@example.com"),
    ]
    password_candidates = [
        ("css", ".auth0-lock-input-show-password input"),
        ("css", "input[name='password']"),
        ("css", "input#1-password"),
        ("css", "input[type='password']"),
        ("placeholder", "your password"),
    ]

    username = None
    for kind, val in email_candidates:
        username = pick_locator(kind, val)
        if username is not None:
            break
    if username is None:
        raise AuthError("Email input not found")

    password = None
    for kind, val in password_candidates:
        password = pick_locator(kind, val)
        if password is not None:
            break
    if password is None:
        raise AuthError("Password input not found")

    username.click()
    micro_pause()
    username.fill(user)
    print("login: filled email")
    micro_pause()
    password.click()
    password.fill("")
    password.type(pw, delay=random.randint(40, 110))
    print("login: filled password")
    micro_pause()

    login_btn = None
    try:
        if auth0_container is not None:
            candidate = auth0_container.get_by_role(
                "button", name=re.compile("^log.?in$", re.I)
            )
            if candidate.count() > 0:
                login_btn = candidate.first
    except Exception:
        pass
    if login_btn is None:
        candidate = page.get_by_role("button", name=re.compile("^log.?in$", re.I))
        if candidate.count() > 0:
            login_btn = candidate.first
    if login_btn is None and auth0_container is not None:
        candidate = auth0_container.locator(".auth0-lock-submit button")
        if candidate.count() > 0:
            login_btn = candidate.first
    if login_btn is None:
        candidate = page.locator(".auth0-lock-submit button")
        if candidate.count() > 0:
            login_btn = candidate.first
    if login_btn is None and auth0_container is not None:
        candidate = auth0_container.locator("button[type='submit']")
        if candidate.count() > 0:
            login_btn = candidate.first
    if login_btn is None:
        candidate = page.locator("button[type='submit']")
        if candidate.count() > 0:
            login_btn = candidate.first
    if login_btn is None:
        raise AuthError("Login button not found")

    login_btn.click()
    print("login: submitted")
    page.wait_for_load_state("networkidle", timeout=60000)
    micro_pause()

    if detect_captcha(page):
        raise CaptchaError("CAPTCHA encountered after login submit")

    error_selectors = [
        ".auth0-global-message",
        ".auth0-lock-error-invalid-hint",
    ]
    error_messages = []
    for sel in error_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                msg = loc.first.inner_text().strip()
                if msg:
                    error_messages.append(msg)
        except Exception:
            continue

    login_still_required = False
    try:
        login_still_required = needs_login(page)
    except CaptchaError:
        raise
    except Exception:
        pass
    login_failed = login_still_required or bool(error_messages)

    if login_failed:
        try:
            page.screenshot(path="login_failed.png", full_page=True)
        except Exception as screenshot_error:
            print(f"login: screenshot failed: {screenshot_error}")
        for msg in error_messages:
            print("login: error banner:", msg)
        return False

    return True


def ensure_authenticated(page, context, relog_state, force=False):
    try:
        login_required = needs_login(page)
    except CaptchaError:
        raise
    if not login_required and not force:
        return
    if not login_required and force:
        return
    if detect_captcha(page):
        raise CaptchaError("CAPTCHA encountered")
    if relog_state.get("attempted"):
        raise AuthError("Authentication required again after retry")
    relog_state["attempted"] = True
    success = perform_login(page)
    if not success:
        raise AuthError("Login failed")
    context.storage_state(path=str(STATE_FILE))
    micro_pause()

def go_to_available_duties(page, keep_auth):
    keep_auth()
    print("navigating to Available Bank Duties")
    # Side menu → "Available Bank Duties"
    # Many Allocate skins use role="link" with visible name; fallback to text search.
    try:
        page.get_by_role("link", name=re.compile("Rostering", re.I)).click(timeout=4000)
        micro_pause()
        keep_auth()
    except PWTimeout:
        pass
    page.get_by_role("link", name=re.compile("Available Bank Duties", re.I)).click(timeout=15000)
    micro_pause()
    keep_auth()
    page.wait_for_selector("table", timeout=15000)
    keep_auth()

def read_table_rows(page):
    # Build header → index map
    headers = [page.locator("table thead th").nth(i).inner_text().strip()
               for i in range(page.locator("table thead th").count())]
    def col(rx):
        for i,h in enumerate(headers):
            if re.search(rx, h, re.I):
                return i
        return None
    idx = {
        "request_id": col(r"Request ID"),
        "day":        col(r"Day"),
        "date":       col(r"Date"),
        "start_end":  col(r"Start-?End"),
        "shift":      col(r"Shift"),
        "unit":       col(r"Unit"),
        "location":   col(r"Location"),
        "grade":      col(r"Grade"),
    }
    rows = []
    body = page.locator("table tbody tr")
    for r in range(body.count()):
        def cell(i):
            return body.nth(r).locator("td").nth(i).inner_text().strip() if i is not None else ""
        row = {k: cell(v) for k, v in idx.items()}
        row["start_end"] = re.sub(r"\s+", " ", row["start_end"])
        rows.append(row)
    return rows

def paginate_collect(page, keep_auth):
    all_rows = []
    while True:
        keep_auth()
        all_rows.extend(read_table_rows(page))
        # try to find a "Next" control; various Allocate themes vary
        next_btn = page.get_by_role("button", name=re.compile(r"(next|›|>)", re.I))
        # If there are numbered pages, click the next numeric if present
        if next_btn.count() == 0:
            # fallback: find a button with aria-label next
            next_btn = page.locator("[aria-label*='Next' i]")
        if next_btn.count() == 0 or ("disabled" in (next_btn.get_attribute("class") or "").lower()):
            break
        try:
            next_btn.first.click(timeout=1500)
            micro_pause()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("table", timeout=10000)
        except Exception:
            break
    return all_rows

def get_period_widget(page):
    # Support either <select> or a button that opens a listbox
    # 1) Try select near "Choose Period"
    sel = page.locator("select").filter(has_text=re.compile("Choose Period", re.I))
    if sel.count() == 0:
        # often it's a sibling select; be generous:
        sel = page.locator("select")
    if sel.count() > 0 and sel.first.locator("option").count() >= 1:
        options = []
        opts = sel.first.locator("option")
        for i in range(opts.count()):
            o = opts.nth(i)
            options.append({"value": o.get_attribute("value") or o.inner_text().strip(),
                            "label": o.inner_text().strip()})
        return ("select", sel.first, options)
    # 2) Fallback: a button opens a menu
    button = page.get_by_text(re.compile("Choose Period", re.I)).locator("xpath=following::*[self::button or @role='button'][1]")
    button.click()
    menu = page.locator("[role='listbox'], ul[role='menu']")
    items = menu.locator("[role='option'], li[role='menuitem']")
    labels = [items.nth(i).inner_text().strip() for i in range(items.count())]
    page.keyboard.press("Escape")
    return ("menu", button, labels)

def select_period(page, widget, item):
    kind, handle, _ = widget
    if kind == "select":
        handle.select_option(item["value"])
    else:
        handle.click()
        micro_pause()
        page.get_by_role("option", name=re.compile(re.escape(item), re.I)).click()
    micro_pause()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("table", timeout=15000)

def scrape_all_periods(page, keep_auth):
    widget = get_period_widget(page)
    options = widget[2]
    all_rows = []
    for item in options:
        keep_auth()
        select_period(page, widget, item)
        keep_auth()
        all_rows.extend(paginate_collect(page, keep_auth))
    return all_rows

def match_action(r, rules):
    def in_list(val, arr):
        v = (val or "").lower()
        return any((a or "").lower() in v for a in arr) if arr else False
    for rule in rules:
        unit_ok = True
        grade_ok = True
        shift_ok = True
        if "unit_in" in rule:
            unit_ok = in_list(r.get("unit"), rule.get("unit_in", []))
        if "grade_in" in rule:
            grade_ok = any(g.lower() == (r.get("grade","").lower()) for g in rule.get("grade_in", []))
        if "start_end_contains_any" in rule:
            shift_ok = in_list(r.get("start_end"), rule.get("start_end_contains_any", []))
        if unit_ok and grade_ok and shift_ok:
            return rule.get("action", "ignore")
    return "ignore"

def main():
    jitter_sleep()

    rules = load_rules()
    seen = load_seen()

    relog_state = {"attempted": False}

    with sync_playwright() as p:
        browser, context = new_context(p)
        page = context.new_page()
        try:
            page.goto(START_URL, wait_until="networkidle", timeout=60000)
            micro_pause()
            ensure_authenticated(page, context, relog_state, force=True)
            context.storage_state(path=str(STATE_FILE))

            def keep_auth():
                ensure_authenticated(page, context, relog_state)

            keep_auth()
            go_to_available_duties(page, keep_auth)
            keep_auth()
            rows = scrape_all_periods(page, keep_auth)
            context.storage_state(path=str(STATE_FILE))
        except CaptchaError as ce:
            send_email("⚠️ CAPTCHA encountered – manual login needed", f"<p>{str(ce)}</p>")
            raise
        except AuthError as ae:
            send_email("⚠️ Re-auth required (Loop)",
                       f"<p>{str(ae)}</p><p><a href='{START_URL}'>Log in to Loop</a></p>")
            raise
        finally:
            context.close()
            browser.close()

    # Deduplicate & detect new
    current_ids = {r.get("request_id") for r in rows if r.get("request_id")}
    new_rows = [r for r in rows if r.get("request_id") and r["request_id"] not in seen]

    if not new_rows:
        # nothing new → just update seen and exit quietly
        save_seen(current_ids | seen)
        return

    # Apply rules
    priority = []
    late = []
    for r in new_rows:
        action = match_action(r, rules)
        if action == "priority":
            priority.append(r)
        elif action == "late":
            late.append(r)

    # Only email if at least one group has content
    if priority:
        send_email(
            subject=f"🔥 New priority shifts ({len(priority)})",
            html=f"<h3>Priority</h3>{fmt_ul(priority)}"
        )
    if late:
        send_email(
            subject=f"🌙 New late/night shifts ({len(late)})",
            html=f"<h3>Late/Night</h3>{fmt_ul(late)}"
        )

    save_seen(current_ids | seen)

if __name__ == "__main__":
    try:
        main()
    except CaptchaError:
        # already handled above
        pass
    except AuthError:
        # already handled above
        pass
    except Exception:
        # fail-safe email so you know it broke
        try:
            send_email("⚠️ Shift scraper failed", f"<pre>{traceback.format_exc()}</pre>")
        except Exception:
            pass
        raise
