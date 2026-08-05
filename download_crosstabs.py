"""
Download all custom crosstabs for a Decipher/Forsta survey as Excel files.

Container-friendly variant: reads its session file and output directory from
environment variables (falling back to local paths so it also runs standalone
outside Docker), and never opens a visible browser window -- login always
happens out-of-band (see README.md) since Decipher requires 2FA.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
AUTH_FILE = Path(os.environ.get("AUTH_STATE_PATH", SCRIPT_DIR / "auth_state.json"))
DOWNLOADS_ROOT = Path(os.environ.get("DOWNLOADS_DIR", SCRIPT_DIR / "downloads"))
NAV_TIMEOUT = 30_000
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def parse_survey_url(url: str):
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Not a full URL (missing scheme/host): {url}")
    match = re.search(r"/apps/report/(.+?)/?$", parts.path)
    if not match:
        raise ValueError(f"Could not find a survey path (/apps/report/...) in URL: {url}")
    origin = f"{parts.scheme}://{parts.netloc}"
    survey_path = match.group(1).strip("/")
    return origin, survey_path


def sanitize_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip()
    return cleaned or "crosstab"


def is_login_page(page) -> bool:
    return page.locator('input[type="password"]').count() > 0


class LoginRequired(Exception):
    pass


class BrowserMissing(Exception):
    """Playwright is installed but its browser binary isn't."""


def launch_chromium(playwright, **kwargs):
    """Launch Chromium, turning the missing-binary case into something readable.

    Playwright otherwise raises a multi-line ASCII-art box telling the user to run
    `playwright install`, which ends up rendered verbatim in the page's error line.
    """
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as exc:                         # noqa: BLE001 - inspected and re-raised
        if "Executable doesn't exist" in str(exc):
            raise BrowserMissing(
                "דפדפן Playwright אינו מותקן בסביבה זו. בקונטיינר הוא מותקן מראש; "
                "בהרצה מקומית הריצו: python -m playwright install chromium"
            ) from exc
        raise


def ensure_logged_in(page, list_url: str):
    page.goto(list_url, wait_until="networkidle", timeout=NAV_TIMEOUT)
    if is_login_page(page):
        # Surfaced verbatim in the page, which is Hebrew.
        raise LoginRequired(
            "פג תוקף החיבור ל-Decipher. הריצו מחדש את שלב ההתחברות בסקריפט הדסקטופ "
            "והעלו כאן את קובץ auth_state.json המעודכן. התחברות אוטומטית אינה אפשרית "
            "מכיוון ש-Decipher דורש אימות דו-שלבי."
        )
    page.wait_for_selector("text=New Crosstab", timeout=NAV_TIMEOUT)


def load_all_cards(page):
    """The crosstabs list lazy-loads more cards as you scroll (Angular endless-scroll).
    Keep scrolling until the card count stops growing."""
    page.wait_for_selector("div.xtab.ng-scope", timeout=NAV_TIMEOUT)
    previous_count = -1
    stable_rounds = 0
    for _ in range(50):
        current_count = page.locator("div.xtab.ng-scope").count()
        if current_count == previous_count:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        previous_count = current_count
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(300)


def list_crosstab_cards(page):
    load_all_cards(page)
    cards = page.locator("div.xtab.ng-scope")
    items = []
    for i in range(cards.count()):
        card = cards.nth(i)
        name = card.locator(".xtab-name").inner_text().strip()
        full_text = card.inner_text()
        is_automatic = "Modified By: Automatic" in full_text
        items.append({"index": i, "name": name, "automatic": is_automatic})
    return items


def download_one_crosstab(page, list_url: str, index: int, expected_name: str, output_dir: Path):
    page.goto(list_url, wait_until="networkidle", timeout=NAV_TIMEOUT)
    load_all_cards(page)
    cards = page.locator("div.xtab.ng-scope")
    card = cards.nth(index)
    actual_name = card.locator(".xtab-name").inner_text().strip()
    if actual_name != expected_name:
        raise RuntimeError(
            f"Crosstab list order changed mid-run (expected '{expected_name}', found '{actual_name}'). "
            "Re-run the job."
        )
    card.click()
    page.wait_for_selector("text=Exports", timeout=NAV_TIMEOUT)
    page.click("text=Exports")
    excel_link = page.locator('a[href*=":export?format=excel"]').first
    excel_link.wait_for(state="visible", timeout=NAV_TIMEOUT)
    with page.expect_download() as download_info:
        excel_link.click()
    download = download_info.value

    safe_name = sanitize_filename(expected_name)
    dest = output_dir / f"{safe_name}.xlsx"
    download.save_as(dest)  # overwrites any previous download/trimmed file with this name
    return dest


def download_all(page, list_url: str, output: Path = None, root: Path = None, progress=None) -> Path:
    """Download every custom crosstab using a page that is already open and authenticated.

    Split out from run_download so the interactive login can carry straight on into the
    download in the browser the user just logged in with, instead of saving the session,
    closing that browser and opening a second one to redo the same navigation. It also
    avoids depending on storage_state() being a complete picture of the session -- it does
    not carry sessionStorage, so a re-created browser is not guaranteed equivalent.

    output pins the folder outright; root instead keeps the survey-named subfolder (which
    also names the combined workbook) but places it under a caller-chosen parent -- the web
    app uses this to give each job its own directory so concurrent runs can't overwrite
    each other's files.
    """
    def log(msg):
        if progress:
            progress(msg)

    survey_name = page.title().strip() or "survey"
    output_dir = Path(output) if output else Path(root or DOWNLOADS_ROOT) / sanitize_filename(survey_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_cards = list_crosstab_cards(page)
    custom_cards = [c for c in all_cards if not c["automatic"]]

    if not custom_cards:
        raise RuntimeError("לא נמצאו קרוסטאבים מותאמים אישית בסקר הזה.")

    log(f"Found {len(custom_cards)} custom crosstab(s) out of {len(all_cards)} total.")

    failures = []
    for item in custom_cards:
        log(f"Downloading: {item['name']} ...")
        try:
            dest = download_one_crosstab(page, list_url, item["index"], item["name"], output_dir)
            log(f"Saved: {dest.name}")
        except Exception as exc:                     # noqa: BLE001 - reported per crosstab
            log(f"FAILED: {item['name']} ({exc})")
            failures.append(item["name"])

    if failures:
        raise RuntimeError(f"Failed to download: {', '.join(failures)}")

    return output_dir


def survey_list_url(survey_url: str) -> str:
    origin, survey_path = parse_survey_url(survey_url)
    return f"{origin}/apps/report/{survey_path}#!/"


def run_download(survey_url: str, output: Path = None, root: Path = None, progress=None) -> Path:
    """Download every custom crosstab for a survey as .xlsx, in a browser of our own.

    Used when there is no live login to piggyback on -- i.e. the session came from an
    uploaded auth_state.json, or from a login earlier on. Returns the output directory and
    raises LoginRequired if the stored session is missing or expired."""
    if not AUTH_FILE.exists():
        raise LoginRequired(
            "לא נטען קובץ התחברות. הריצו את שלב ההתחברות בסקריפט הדסקטופ "
            "והעלו כאן את קובץ auth_state.json."
        )

    list_url = survey_list_url(survey_url)

    with sync_playwright() as playwright:
        browser = launch_chromium(playwright, headless=True)
        context = browser.new_context(storage_state=str(AUTH_FILE), accept_downloads=True)
        page = context.new_page()
        try:
            ensure_logged_in(page, list_url)
            return download_all(page, list_url, output=output, root=root, progress=progress)
        finally:
            browser.close()
