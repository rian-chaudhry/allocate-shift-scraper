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
ARTIFACTS_DIR = ROOT / "artifacts"
VIDEO_TEMP_DIR = ARTIFACTS_DIR / "video"

USER_AGENTS = [
    # keep a few realistic UAs; Playwright already does a lot, this just adds variation
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def jitter_sleep():
    # Shorter, still human-like jitter: 4–37 seconds
    delay = random.uniform(4, 37)
    print(f"jitter: sleeping {delay:.1f}s before scrape")
    time.sleep(delay)


def micro_pause():
    time.sleep(random.uniform(0.3, 1.1))

def ensure_artifact_dirs():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_TEMP_DIR.mkdir(parents=True, exist_ok=True)

def capture_artifacts(page, name):
    ensure_artifact_dirs()
    png_path = ARTIFACTS_DIR / f"{name}.png"
    html_path = ARTIFACTS_DIR / f"{name}.html"
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as exc:
        print(f"artifact capture failed for {name}.png: {exc}")
    try:
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception as exc:
        print(f"artifact capture failed for {name}.html: {exc}")

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
    ensure_artifact_dirs()
    args = {"user_agent": ua, "viewport": vp, "locale": "en-GB", "record_video_dir": str(VIDEO_TEMP_DIR)}
    if STATE_FILE.exists():
        args["storage_state"] = str(STATE_FILE)
    context = browser.new_context(**args)
    context.add_init_script("try{ sessionStorage.setItem('setPhoneLogin','false'); }catch(e){}")
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

    start_time = time.monotonic()
    deadline = start_time + 90
    bounce_retry_used = False

    def visible_or_none(locator):
        try:
            count = locator.count()
        except Exception:
            return None
        for i in range(count):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    def find_visible_input(selectors, container=None):
        for sel in selectors:
            scopes = []
            if container is not None:
                scopes.append(container.locator(sel))
            scopes.append(page.locator(sel))
            for scope in scopes:
                try:
                    count = scope.count()
                except Exception:
                    continue
                for idx in range(count):
                    candidate = scope.nth(idx)
                    try:
                        candidate.wait_for(state="visible", timeout=60000)
                        return candidate
                    except Exception:
                        continue
        return None

    while True:
        if time.monotonic() > deadline:
            capture_artifacts(page, "after_login")
            raise AuthError("Login failed")

        if detect_captcha(page):
            capture_artifacts(page, "after_login")
            raise CaptchaError("CAPTCHA encountered during login")

        welcome_login_btn = visible_or_none(
            page.get_by_role("button", name=re.compile("log.?in", re.I))
        )
        if welcome_login_btn is not None:
            capture_artifacts(page, "welcome")
            try:
                welcome_login_btn.click()
                print("clicked Log In")
            except Exception:
                pass

        time_left = deadline - time.monotonic()
        if time_left <= 0:
            capture_artifacts(page, "after_login")
            raise AuthError("Login failed")
        timeout_ms = int(min(60000, max(1000, time_left * 1000)))
        container_selector = ".auth0-lock-form, .auth0-lock-cred-pane-internal-wrapper"
        try:
            page.wait_for_selector(
                container_selector,
                state="visible",
                timeout=timeout_ms,
            )
        except PWTimeout:
            capture_artifacts(page, "after_login")
            raise AuthError("Auth0 Lock form did not appear")

        container_candidates = page.locator(container_selector)
        auth0_container = container_candidates.first if container_candidates.count() > 0 else None

        try:
            page.evaluate("sessionStorage.setItem('setPhoneLogin','false')")
        except Exception:
            pass
        micro_pause()
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        micro_pause()

        try:
            page.wait_for_selector(
                container_selector,
                state="visible",
                timeout=timeout_ms,
            )
        except PWTimeout:
            capture_artifacts(page, "after_login")
            raise AuthError("Auth0 Lock form did not reappear")
        container_candidates = page.locator(container_selector)
        auth0_container = container_candidates.first if container_candidates.count() > 0 else None

        email_input = find_visible_input(
            [
                "input[type='email']",
                "input[name='email']",
                "input[autocomplete='username']",
                "input[type='text'][name='username']",
                ".auth0-lock-input input",
            ],
            container=auth0_container,
        )
        if email_input is None:
            toggle = page.locator("#btnLoginPhone, button:has-text('Login with username'), button:has-text('Login with phone number')")
            try:
                if toggle.count() > 0 and toggle.first.is_visible():
                    toggle.first.click()
                    micro_pause()
                    try:
                        page.evaluate("sessionStorage.setItem('setPhoneLogin','false')")
                    except Exception:
                        pass
                    micro_pause()
                    try:
                        page.reload(wait_until='domcontentloaded')
                    except Exception:
                        pass
                    micro_pause()
                    try:
                        page.wait_for_selector(
                            container_selector,
                            state="visible",
                            timeout=timeout_ms,
                        )
                    except PWTimeout:
                        capture_artifacts(page, "after_login")
                        raise AuthError("Auth0 Lock form did not reappear after toggle")
                    container_candidates = page.locator(container_selector)
                    auth0_container = (
                        container_candidates.first if container_candidates.count() > 0 else None
                    )
                    email_input = find_visible_input(
                        [
                            "input[type='email']",
                            "input[name='email']",
                            "input[autocomplete='username']",
                            "input[type='text'][name='username']",
                            ".auth0-lock-input input",
                        ],
                        container=auth0_container,
                    )
            except Exception:
                pass
        if email_input is None:
            capture_artifacts(page, "auth0_debug")
            capture_artifacts(page, "after_login")
            raise AuthError("Email input not found")

        password_input = find_visible_input(
            [
                "input[type='password']",
                "input[name='password']",
                ".auth0-lock-input input[type='password']",
            ],
            container=auth0_container,
        )
        if password_input is None:
            capture_artifacts(page, "auth0_debug")
            capture_artifacts(page, "after_login")
            raise AuthError("Password input not found")

        try:
            email_input.wait_for(state="visible", timeout=60000)
            email_input.click()
            micro_pause()
            email_input.fill(user)
            print("login: filled email")
        except Exception as exc:
            capture_artifacts(page, "after_login")
            raise AuthError(f"Unable to fill email input: {exc}")

        try:
            password_input.wait_for(state="visible", timeout=60000)
            password_input.click()
            password_input.fill("")
            password_input.type(pw, delay=random.randint(40, 110))
            print("login: filled password")
        except Exception as exc:
            capture_artifacts(page, "after_login")
            raise AuthError(f"Unable to fill password input: {exc}")

        capture_artifacts(page, "auth0_filled")

        login_button = None
        login_candidates = [
            page.get_by_role("button", name=re.compile("^log.?in$", re.I)),
            page.locator(".auth0-lock-submit button"),
            page.locator("button[type='submit']"),
        ]
        if auth0_container is not None:
            login_candidates = [
                auth0_container.get_by_role("button", name=re.compile("^log.?in$", re.I)),
                auth0_container.locator(".auth0-lock-submit button"),
                auth0_container.locator("button[type='submit']"),
            ] + login_candidates
        for candidate in login_candidates:
            locator = visible_or_none(candidate)
            if locator is not None:
                login_button = locator
                break
        if login_button is None:
            capture_artifacts(page, "auth0_debug")
            capture_artifacts(page, "after_login")
            raise AuthError("Login button not found")

        try:
            login_button.wait_for(state="visible", timeout=60000)
            login_button.click()
            print("login: submitted")
        except Exception as exc:
            capture_artifacts(page, "after_login")
            raise AuthError(f"Unable to click login button: {exc}")

        capture_artifacts(page, "auth0_submitted")

        submit_time = time.monotonic()
        post_submit_captured = False
        bounce_triggered = False

        def maybe_capture_post_submit(force=False):
            nonlocal post_submit_captured
            if post_submit_captured:
                return
            elapsed = time.monotonic() - submit_time
            if elapsed >= 10 or force:
                wait_needed = max(0, 10 - elapsed) if force and elapsed < 10 else 0
                if wait_needed > 0:
                    available = max(0, deadline - time.monotonic())
                    sleep_for = min(wait_needed, available)
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                capture_artifacts(page, "post_submit_wait")
                post_submit_captured = True

        while True:
            now = time.monotonic()
            if now > deadline:
                maybe_capture_post_submit(force=True)
                capture_artifacts(page, "after_login")
                raise AuthError("Login failed")

            maybe_capture_post_submit()

            if detect_captcha(page):
                maybe_capture_post_submit(force=True)
                capture_artifacts(page, "after_login")
                raise CaptchaError("CAPTCHA encountered after login submit")

            current_url = page.url or ""
            success = False
            if current_url.startswith(f"{BASE_URL}/loop"):
                try:
                    rostering_tab = page.get_by_role("tab", name=re.compile("Rostering", re.I))
                    if rostering_tab.count() > 0 and rostering_tab.first.is_visible():
                        success = True
                except Exception:
                    pass
            if not success:
                try:
                    duties_link = page.get_by_role("link", name=re.compile("Available Bank Duties", re.I))
                    if duties_link.count() > 0 and duties_link.first.is_visible():
                        success = True
                except Exception:
                    pass
            if success:
                maybe_capture_post_submit(force=True)
                capture_artifacts(page, "after_login")
                return True

            error_message = None
            error_sources = []
            if auth0_container is not None:
                error_sources.append(auth0_container)
            error_sources.append(page.locator(".auth0-lock"))
            error_sources.append(page)
            for source in error_sources:
                try:
                    error_candidate = source.locator("text=/(invalid|wrong|try again)/i")
                    if error_candidate.count() > 0:
                        candidate = visible_or_none(error_candidate)
                        if candidate is not None:
                            text = candidate.inner_text().strip()
                            if text:
                                error_message = text
                                break
                except Exception:
                    continue
            if error_message:
                maybe_capture_post_submit(force=True)
                capture_artifacts(page, "after_login")
                raise AuthError(f"Login error: {error_message}")

            if not bounce_retry_used and now - submit_time >= 20:
                welcome_again = visible_or_none(
                    page.get_by_role("button", name=re.compile("log.?in", re.I))
                )
                if welcome_again is not None:
                    print("login: bounce detected, retrying welcome card")
                    capture_artifacts(page, "welcome")
                    try:
                        welcome_again.click()
                        micro_pause()
                    except Exception:
                        pass
                    bounce_retry_used = True
                    bounce_triggered = True
                    break

            time.sleep(random.uniform(0.5, 0.8))

        if bounce_triggered:
            continue


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
    perform_login(page)
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
        ensure_artifact_dirs()
        console_log_path = ARTIFACTS_DIR / "browser-console.log"
        console_log_path.write_text("", encoding="utf-8")

        def append_console(msg):
            try:
                ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                location = msg.location
                where = f"{location.get('url', '')}:{location.get('lineNumber', '')}" if location else ""
                with console_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{ts}\t{msg.type}\t{where}\t{msg.text}\n")
            except Exception as exc:
                print(f"console log write failed: {exc}")

        page.on("console", append_console)
        page_video = getattr(page, "video", None)
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
            try:
                context.close()
            finally:
                browser.close()
            if page_video is not None:
                try:
                    raw_path = Path(page_video.path())
                    target_path = ARTIFACTS_DIR / "login.webm"
                    if raw_path.exists():
                        try:
                            if target_path.exists():
                                target_path.unlink()
                            raw_path.replace(target_path)
                        except Exception:
                            target_path.write_bytes(raw_path.read_bytes())
                            raw_path.unlink(missing_ok=True)
                except Exception as exc:
                    print(f"login video capture failed: {exc}")

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
