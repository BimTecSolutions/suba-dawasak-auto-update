"""
Suba Dawasak — GitHub Actions updater (built for a 5-minute schedule).

Firestore cost per run:
  * Nothing new published : 1 read, 0 writes
  * Each new draw         : 1 write (create(); never overwrites existing data)

Efficiency:
  * No meta document and no "exists?" reads: the latest draw number comes from
    ONE ordered query (limit 1), which can never go stale.
  * The URL style (padded vs not, which host) is copied from the latest stored
    document, so a normal run requests ONE page per draw number.
  * Plain HTTP first. A clean 404 (not published) never starts Chrome.
    Chrome is capped at MAX_SELENIUM_FETCHES page loads per run.
  * Stops after MAX_CONSECUTIVE_MISSES unpublished draws in a row.

Environment variables (all optional):
  GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_CREDENTIALS   service account JSON path
  START_DRAW_FALLBACK      first draw if Firestore is empty. Default: 1
  MAX_CONSECUTIVE_MISSES   Default: 2
  MAX_NEW_DRAWS_PER_RUN    Default: 20
  MAX_SELENIUM_FETCHES     Default: 3
  ALL_URL_VARIANTS         1 = try every URL variant (slower). Default: 0
  DEBUG                    1 = print page text when parsing fails
"""

import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists


class SubaDawasakUpdater:
    BASE_URLS = ["https://www.nlb.lk", "https://r.nlb.lk"]

    LOTTERY_SLUG = "suba-dawasak"
    LOTTERY_NAME = "Suba Dawasak"

    FIRESTORE_COLLECTION = "NLB_Results_All"
    FIRESTORE_DOCUMENT = "suba-dawasak"
    FIRESTORE_SUBCOLLECTION = "results"

    HTTP_TIMEOUT = 20
    PAGE_WAIT = 4

    ZODIAC_SIGNS = [
        "ARIES", "TAURUS", "GEMINI", "CANCER", "LEO", "VIRGO",
        "LIBRA", "SCORPIO", "SAGITTARIUS", "CAPRICORN",
        "AQUARIUS", "PISCES",
    ]

    def __init__(
        self,
        creds_path: str,
        debug: bool = False,
        max_selenium_fetches: int = 3,
        all_url_variants: bool = False,
    ):
        self.debug = debug
        self.max_selenium_fetches = max_selenium_fetches
        self.all_url_variants = all_url_variants
        self.selenium_fetches = 0
        self.driver = None
        self.url_base: Optional[str] = None      # learned from latest stored doc
        self.url_padded: bool = True

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nlb.lk/",
            "Connection": "keep-alive",
        })

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(creds_path))
        self.db = firestore.client()
        print("✅ Firebase initialized.")

    # ------------------------------------------------------------
    # FIREBASE
    # ------------------------------------------------------------
    def results_collection(self):
        return (
            self.db.collection(self.FIRESTORE_COLLECTION)
            .document(self.FIRESTORE_DOCUMENT)
            .collection(self.FIRESTORE_SUBCOLLECTION)
        )

    def get_latest_draw_number(self) -> Optional[int]:
        """ONE read: the stored document with the highest draw_number."""
        docs = list(
            self.results_collection()
            .order_by("draw_number", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not docs:
            return None

        data = docs[0].to_dict() or {}
        self._learn_url_style(data.get("url"))

        latest = data.get("draw_number")
        if isinstance(latest, int):
            return latest
        try:
            return int(latest)
        except (TypeError, ValueError):
            return None

    def _learn_url_style(self, url: Optional[str]):
        """Remember host + padded/unpadded style from the latest stored URL."""
        if not url or f"/results/{self.LOTTERY_SLUG}/" not in url:
            return
        base, _, last = url.rstrip("/").rpartition("/")
        if not last.isdigit():
            return
        self.url_base = base
        self.url_padded = len(last) >= 4 and last.startswith("0")

    def upload_new_result(self, result: Dict) -> bool:
        """create() = 1 write, fails if the document already exists."""
        doc_id = f"suba-dawasak_{result['draw_number_str']}"
        try:
            self.results_collection().document(doc_id).create(result)
            print(f"   ☁️ Uploaded: {doc_id}")
            return True
        except AlreadyExists:
            print(f"   ℹ️ {doc_id} already exists — left unchanged.")
            return False
        except Exception as e:
            print(f"   ❌ Firestore upload failed: {e}")
            return False

    # ------------------------------------------------------------
    # FETCHING (cheap HTTP first, Chrome only when needed)
    # ------------------------------------------------------------
    def fetch_with_requests(self, url: str) -> Tuple[Optional[str], Optional[int]]:
        for attempt in (1, 2):
            try:
                resp = self.session.get(url, timeout=self.HTTP_TIMEOUT, allow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 2000:
                    return resp.text, 200
                return None, resp.status_code
            except Exception as e:
                print(f"   ⚠️ Request error: {e}")
                if attempt == 1:
                    time.sleep(2)
        return None, None

    def init_selenium(self):
        if self.driver:
            return self.driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(45)
            print("✅ Selenium Chrome initialized.")
            return self.driver
        except Exception as e:
            print(f"❌ Selenium initialization failed: {e}")
            return None

    def fetch_with_selenium(self, url: str) -> Optional[str]:
        if self.selenium_fetches >= self.max_selenium_fetches:
            print("   ⏭️ Chrome fetch limit for this run reached.")
            return None

        driver = self.init_selenium()
        if not driver:
            return None

        self.selenium_fetches += 1
        try:
            driver.get(url)
            time.sleep(self.PAGE_WAIT)
            html = driver.page_source
            if len(html) > 2000 and "draw" in html.lower():
                return html
        except Exception as e:
            print(f"   ⚠️ Selenium error: {e}")
        return None

    # ------------------------------------------------------------
    # TEXT HELPERS
    # ------------------------------------------------------------
    @staticmethod
    def normalize_text(text: str) -> str:
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def get_page_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return self.normalize_text(soup.get_text(separator=" ", strip=True))

    # ------------------------------------------------------------
    # DATE (same extract_date that worked in your backfill run)
    # ------------------------------------------------------------
    def extract_date(self, text: str) -> Optional[str]:
        # Matches "Date: Tuesday September 01, 2026" (no comma after weekday)
        m = re.search(
            r"(?<![A-Za-z])Date\s*[:\-]\s*"
            r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?"
            r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
            text,
        )
        if m:
            parsed = self._parse_date_value(m.group(1))
            if parsed:
                return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
        return None

    @staticmethod
    def _parse_date_value(date_string: Optional[str]) -> Optional[datetime]:
        if not date_string:
            return None
        cleaned = re.sub(r"\s+", " ", date_string.strip())
        cleaned = re.sub(
            r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
            "", cleaned, flags=re.IGNORECASE,
        )
        # FIX: only rewrite the standalone word "Sept". A plain
        # .replace("Sept", "Sep") also corrupts "September" -> "Sepember".
        cleaned = re.sub(r"\bSept\b", "Sep", cleaned).replace(".", "")
        for fmt in (
            "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
            "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y",
            "%m/%d/%Y", "%m-%d-%Y",
        ):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return None

    def parse_date(self, date_string: Optional[str]) -> Optional[str]:
        parsed = self._parse_date_value(date_string)
        return parsed.strftime("%Y-%m-%dT00:00:00") if parsed else None

    # ------------------------------------------------------------
    # RESULT PARSING (promotional draw optional)
    # ------------------------------------------------------------
    def _zodiac_regex(self) -> str:
        signs = sorted(self.ZODIAC_SIGNS, key=len, reverse=True)
        return r"\b(" + "|".join(signs) + r")\b"

    @staticmethod
    def _pack_main(zodiac: str, raw_numbers: List[str]) -> Dict:
        numbers = [f"{int(x):02d}" for x in raw_numbers]
        return {
            "zodiac": zodiac.upper(),
            "numbers": numbers,
            "numbers_int": [int(x) for x in numbers],
        }

    def extract_main_result(self, text: str) -> Optional[Dict]:
        zodiac_regex = self._zodiac_regex()

        strict = re.compile(
            zodiac_regex + r"\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})(?!\d)",
            re.IGNORECASE,
        )
        m = strict.search(text)
        if m:
            return self._pack_main(m.group(1), [m.group(2), m.group(3), m.group(4)])

        for zm in re.finditer(zodiac_regex, text, re.IGNORECASE):
            window = text[zm.end(): zm.end() + 80]
            nums = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", window)
            if len(nums) >= 3:
                return self._pack_main(zm.group(1), nums[:3])

        for promo in re.finditer(r"Promotional\s+Draw", text, re.IGNORECASE):
            before = text[max(0, promo.start() - 300): promo.start()]
            zodiacs = list(re.finditer(zodiac_regex, before, re.IGNORECASE))
            if not zodiacs:
                continue
            zm = zodiacs[-1]
            nums = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", before[zm.end():])
            if len(nums) >= 3:
                return self._pack_main(zm.group(1), nums[-3:])

        return None

    def extract_promotional_draw(self, text: str) -> Optional[Dict]:
        for promo in re.finditer(r"Promotional\s+Draw", text, re.IGNORECASE):
            section = text[promo.end(): promo.end() + 150]
            digits = re.findall(r"(?<!\d)([0-9])(?!\d)", section)
            if len(digits) >= 4:
                digits = digits[:4]
                return {"numbers": digits, "numbers_int": [int(x) for x in digits]}
        return None

    @staticmethod
    def page_mentions_draw(text: str, draw_number: int) -> bool:
        return re.search(rf"(?<!\d)0*{draw_number}(?!\d)", text) is not None

    # ------------------------------------------------------------
    # SCRAPE ONE DRAW
    # ------------------------------------------------------------
    def try_parse(self, html: str, url: str, draw_number: int) -> Tuple[Optional[Dict], str]:
        text = self.get_page_text(html)
        if len(text) < 300 or not self.page_mentions_draw(text, draw_number):
            return None, text

        main_result = self.extract_main_result(text)
        if not main_result:
            return None, text

        draw_padded = f"{draw_number:04d}"
        date = self.extract_date(text)
        date_iso = self.parse_date(date)

        # Never store a guessed/unrelated date.
        if not date or not date_iso:
            print(f"   ⚠️ Draw {draw_padded}: explicit draw date not found; not uploading.")
            return None, text

        promotional = self.extract_promotional_draw(text)

        result = {
            "lottery": self.LOTTERY_NAME,
            "lottery_slug": self.LOTTERY_SLUG,
            "draw_number": draw_number,
            "draw_number_str": draw_padded,
            "date": date,
            "date_iso": date_iso,
            "main_result": main_result,
            "has_promotional_draw": promotional is not None,
            "url": url,
            "scraped_at": datetime.now().isoformat(),
            "source": "National Lotteries Board, Sri Lanka",
        }
        if promotional:
            result["promotional_draw"] = promotional
        return result, text

    def candidate_urls(self, draw_number: int) -> List[str]:
        padded = f"{draw_number:04d}"
        plain = str(draw_number)

        # Normal case: exactly one URL, in the style the latest doc used.
        if self.url_base and not self.all_url_variants:
            return [f"{self.url_base}/{padded if self.url_padded else plain}"]

        urls = []
        for base in self.BASE_URLS:
            for value in (padded, plain):
                urls.append(f"{base}/results/{self.LOTTERY_SLUG}/{value}")
        return list(dict.fromkeys(urls))

    def scrape_draw(self, draw_number: int) -> Optional[Dict]:
        draw_padded = f"{draw_number:04d}"
        print(f"\n🔎 Checking draw {draw_padded}...")

        last_text = ""

        for url in self.candidate_urls(draw_number):
            html, status = self.fetch_with_requests(url)

            if status == 404:          # clean "not published" — skip Chrome
                continue

            if html:
                result, text = self.try_parse(html, url, draw_number)
                last_text = text or last_text
                if result:
                    return self._found(result, draw_padded)

            html = self.fetch_with_selenium(url)
            if html:
                result, text = self.try_parse(html, url, draw_number)
                last_text = text or last_text
                if result:
                    return self._found(result, draw_padded)

        print(f"   ⛔ Draw {draw_padded} not available (or could not be parsed).")
        if self.debug and last_text:
            print("   --- DEBUG page text (first 3000 chars) ---")
            print(last_text[:3000])
            print("   --- END DEBUG ---")
        return None

    @staticmethod
    def _found(result: Dict, draw_padded: str) -> Dict:
        main = result["main_result"]
        note = "+promo" if result["has_promotional_draw"] else "no promo"
        print(f"   ✅ Found draw {draw_padded}: {main['zodiac']} "
              f"{'-'.join(main['numbers'])} ({note}) date={result['date']}")
        return result

    # ------------------------------------------------------------
    # INCREMENTAL UPDATE
    # ------------------------------------------------------------
    def run(self, start_fallback: int, max_misses: int, max_new: int) -> int:
        latest = self.get_latest_draw_number()

        if latest is None:
            print(f"📭 No results stored. Starting from {start_fallback:04d}.")
            next_draw = start_fallback
        else:
            print(f"📌 Latest draw in Firestore: {latest:04d}")
            next_draw = latest + 1

        uploaded = 0
        misses = 0
        attempts = 0

        while misses < max_misses and attempts < max_new:
            attempts += 1
            result = self.scrape_draw(next_draw)

            if result:
                if self.upload_new_result(result):
                    uploaded += 1
                misses = 0
            else:
                misses += 1

            next_draw += 1

        if uploaded == 0:
            print("\n✅ No new draws — already up to date.")
        else:
            print(f"\n🎉 Uploaded {uploaded} new draw(s).")
        return uploaded

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
                print("🛑 Selenium Chrome closed.")
            except Exception:
                pass
            self.driver = None


def main():
    creds_path = (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("FIREBASE_CREDENTIALS")
        or "serviceAccountKey.json"
    )
    if not os.path.exists(creds_path):
        print(f"❌ Credentials file not found: {creds_path}")
        raise SystemExit(1)

    updater = None
    try:
        updater = SubaDawasakUpdater(
            creds_path,
            debug=os.environ.get("DEBUG", "0") == "1",
            max_selenium_fetches=int(os.environ.get("MAX_SELENIUM_FETCHES", "3")),
            all_url_variants=os.environ.get("ALL_URL_VARIANTS", "0") == "1",
        )
        updater.run(
            start_fallback=int(os.environ.get("START_DRAW_FALLBACK", "1")),
            max_misses=int(os.environ.get("MAX_CONSECUTIVE_MISSES", "2")),
            max_new=int(os.environ.get("MAX_NEW_DRAWS_PER_RUN", "20")),
        )
    finally:
        if updater:
            updater.close()


if __name__ == "__main__":
    main()
