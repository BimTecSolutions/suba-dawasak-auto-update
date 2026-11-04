"""
Suba Dawasak — incremental Firestore updater (robust version).

What changed vs the old version:
  * The main result (zodiac + 3 numbers) no longer REQUIRES the words
    "Promotional Draw" to be on the page. The promotional draw is now
    optional, so draws after 0442 (which have none) are parsed correctly.
  * Parsing uses several fallback strategies instead of one strict regex.
  * The page is checked to really be the requested draw (so a redirect to
    another draw's page is never saved under the wrong number).
  * On a parse failure, the page text is printed (DEBUG=1) so you can see
    exactly why in the GitHub Actions log.
  * Defaults allow catching up on a big backlog (0443 -> 0476) in one run.

Environment variables:
  FIREBASE_CREDENTIALS     Path to service account JSON. Default: serviceAccountKey.json
  START_DRAW_FALLBACK      Start draw if Firestore is empty. Default: 1
  MAX_CONSECUTIVE_MISSES   Stop after this many misses in a row. Default: 4
  MAX_NEW_DRAWS_PER_RUN    Safety cap per run. Default: 60
  DRAW_DELAY_SECONDS       Delay between draws. Default: 1.0
  DEBUG                    Set to 1 to print page text when parsing fails.
"""

import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore


class SubaDawasakScraper:
    BASE_URLS = [
        "https://www.nlb.lk",
        "https://r.nlb.lk",
    ]

    LOTTERY_SLUG = "suba-dawasak"
    LOTTERY_NAME = "Suba Dawasak"

    FIRESTORE_COLLECTION = "NLB_Results_All"
    FIRESTORE_DOCUMENT = "suba-dawasak"
    FIRESTORE_SUBCOLLECTION = "results"

    MAX_REQUEST_RETRIES = 2
    PAGE_WAIT = 4

    ZODIAC_SIGNS = [
        "ARIES", "TAURUS", "GEMINI", "CANCER", "LEO", "VIRGO",
        "LIBRA", "SCORPIO", "SAGITTARIUS", "CAPRICORN",
        "AQUARIUS", "PISCES",
    ]

    def __init__(self, firebase_creds_path: str, debug: bool = False):
        self.db = None
        self.driver = None
        self.debug = debug

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

        self.init_firebase(firebase_creds_path)

    # ------------------------------------------------------------
    # FIREBASE
    # ------------------------------------------------------------
    def init_firebase(self, creds_path: str):
        if not firebase_admin._apps:
            cred = credentials.Certificate(creds_path)
            firebase_admin.initialize_app(cred)

        self.db = firestore.client()
        print("✅ Firebase initialized.")

    def get_results_collection(self):
        return (
            self.db
            .collection(self.FIRESTORE_COLLECTION)
            .document(self.FIRESTORE_DOCUMENT)
            .collection(self.FIRESTORE_SUBCOLLECTION)
        )

    def get_latest_draw_number(self) -> Optional[int]:
        query = (
            self.get_results_collection()
            .order_by("draw_number", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        docs = list(query.stream())
        if not docs:
            return None
        return docs[0].to_dict().get("draw_number")

    def upload_to_firestore(self, result: Dict) -> bool:
        document_id = f"suba-dawasak_{result['draw_number_str']}"
        try:
            self.get_results_collection().document(document_id).set(
                result, merge=True
            )
            print(f"   ☁️ Uploaded: {document_id}")
            return True
        except Exception as e:
            print(f"   ❌ Firestore upload failed: {e}")
            return False

    # ------------------------------------------------------------
    # SELENIUM / REQUESTS
    # ------------------------------------------------------------
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

            try:
                self.driver.execute_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            except Exception:
                pass

            print("✅ Selenium Chrome initialized.")
            return self.driver

        except Exception as e:
            print(f"❌ Selenium initialization failed: {e}")
            return None

    def fetch_with_selenium(self, url: str) -> Optional[str]:
        driver = self.init_selenium()
        if not driver:
            return None

        for attempt in range(1, self.MAX_REQUEST_RETRIES + 1):
            try:
                driver.get(url)
                time.sleep(self.PAGE_WAIT)
                html = driver.page_source

                if len(html) > 2000 and "draw" in html.lower():
                    return html
            except Exception as e:
                print(f"   ⚠️ Selenium error: {e}")

            if attempt < self.MAX_REQUEST_RETRIES:
                time.sleep(attempt * 2)

        return None

    def fetch_with_requests(self, url: str) -> Optional[str]:
        for attempt in range(1, self.MAX_REQUEST_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=30, allow_redirects=True)
                if response.status_code == 200 and len(response.text) > 2000:
                    return response.text
            except Exception as e:
                print(f"   ⚠️ Request error: {e}")

            if attempt < self.MAX_REQUEST_RETRIES:
                time.sleep(attempt)

        return None

    def fetch_page(self, url: str) -> Optional[str]:
        html = self.fetch_with_selenium(url)
        if html:
            return html
        return self.fetch_with_requests(url)

    # ------------------------------------------------------------
    # TEXT HELPERS
    # ------------------------------------------------------------
    @staticmethod
    def normalize_text(text: str) -> str:
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def get_page_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return self.normalize_text(soup.get_text(separator=" ", strip=True))

    # ------------------------------------------------------------
    # DATE
    # ------------------------------------------------------------
    def extract_date(self, text: str) -> Optional[str]:
        patterns = [
            r"Draw\s+Date\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
            r"Date\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
            r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
            r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
            r"((?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|"
            r"Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def parse_date(self, date_string: Optional[str]) -> Optional[str]:
        if not date_string:
            return None

        cleaned = date_string.strip().replace("Sept", "Sep").replace(".", "")
        formats = [
            "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
            "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).isoformat()
            except ValueError:
                continue
        return date_string

    # ------------------------------------------------------------
    # RESULT PARSING
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
        """
        Zodiac sign followed by three numbers. The "Promotional Draw"
        label is NOT required (draws after 0442 don't have one).
        """
        zodiac_regex = self._zodiac_regex()

        # Strategy 1: zodiac immediately followed by three 1-2 digit numbers.
        strict = re.compile(
            zodiac_regex + r"\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})(?!\d)",
            re.IGNORECASE,
        )
        match = strict.search(text)
        if match:
            return self._pack_main(
                match.group(1), [match.group(2), match.group(3), match.group(4)]
            )

        # Strategy 2: zodiac followed (within a short window) by 3 numbers,
        # allowing separators like "-", ",", "|" or small words between them.
        for zm in re.finditer(zodiac_regex, text, re.IGNORECASE):
            window = text[zm.end(): zm.end() + 80]
            nums = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", window)
            if len(nums) >= 3:
                return self._pack_main(zm.group(1), nums[:3])

        # Strategy 3 (old behaviour): numbers BEFORE a "Promotional Draw" label.
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
        """Optional. Returns None when the draw has no promotional part."""
        for promo in re.finditer(r"Promotional\s+Draw", text, re.IGNORECASE):
            section = text[promo.end(): promo.end() + 150]
            digits = re.findall(r"(?<!\d)([0-9])(?!\d)", section)
            if len(digits) >= 4:
                digits = digits[:4]
                return {
                    "numbers": digits,
                    "numbers_int": [int(x) for x in digits],
                }
        return None

    @staticmethod
    def page_mentions_draw(text: str, draw_number: int) -> bool:
        """Guard against redirects to another draw's page."""
        pattern = rf"(?<!\d)0*{draw_number}(?!\d)"
        return re.search(pattern, text) is not None

    # ------------------------------------------------------------
    # SCRAPE ONE DRAW
    # ------------------------------------------------------------
    def scrape_draw(self, draw_number: int) -> Optional[Dict]:
        draw_padded = f"{draw_number:04d}"
        print(f"\n🔎 Checking draw {draw_padded}...")

        urls = []
        for base_url in self.BASE_URLS:
            for number_value in (draw_padded, str(draw_number)):
                urls.append(f"{base_url}/results/{self.LOTTERY_SLUG}/{number_value}")
                urls.append(f"{base_url}/results/{self.LOTTERY_SLUG}/{number_value}/")
        urls = list(dict.fromkeys(urls))

        last_text = ""

        for url in urls:
            html = self.fetch_page(url)
            if not html:
                continue

            text = self.get_page_text(html)
            if len(text) < 300:
                continue
            last_text = text

            if not self.page_mentions_draw(text, draw_number):
                print(f"   ↪️ {url} does not look like draw {draw_padded}, skipping.")
                continue

            main_result = self.extract_main_result(text)
            if not main_result:
                continue

            date = self.extract_date(text)
            promotional = self.extract_promotional_draw(text)

            result = {
                "lottery": self.LOTTERY_NAME,
                "lottery_slug": self.LOTTERY_SLUG,
                "draw_number": draw_number,
                "draw_number_str": draw_padded,
                "date": date,
                "date_iso": self.parse_date(date),
                "main_result": main_result,
                "has_promotional_draw": promotional is not None,
                "url": url,
                "scraped_at": datetime.now().isoformat(),
                "source": "National Lotteries Board, Sri Lanka",
            }

            if promotional:
                result["promotional_draw"] = promotional

            promo_note = " (+promo)" if promotional else " (no promo)"
            print(f"   ✅ Found draw {draw_padded}: {main_result['zodiac']} "
                  f"{'-'.join(main_result['numbers'])}{promo_note}")
            return result

        print(f"   ⛔ Draw {draw_padded} not available (or could not be parsed).")
        if self.debug and last_text:
            print("   --- DEBUG page text (first 1500 chars) ---")
            print(last_text[:1500])
            print("   --- END DEBUG ---")
        return None

    # ------------------------------------------------------------
    # INCREMENTAL UPDATE
    # ------------------------------------------------------------
    def find_and_upload_new_draws(
        self,
        start_draw_fallback: int,
        max_consecutive_misses: int,
        max_new_draws: int,
        delay: float,
    ) -> int:
        latest_known = self.get_latest_draw_number()

        if latest_known is None:
            print(f"📭 No existing results in Firestore. Starting from "
                  f"{start_draw_fallback:04d}.")
            next_draw = start_draw_fallback
        else:
            print(f"📌 Latest draw already in Firestore: {latest_known:04d}")
            next_draw = latest_known + 1

        new_uploaded = 0
        consecutive_misses = 0
        attempts = 0

        while consecutive_misses < max_consecutive_misses and attempts < max_new_draws:
            attempts += 1
            result = self.scrape_draw(next_draw)

            if result:
                if self.upload_to_firestore(result):
                    new_uploaded += 1
                consecutive_misses = 0
            else:
                consecutive_misses += 1

            next_draw += 1
            time.sleep(delay)

        if new_uploaded == 0:
            print("\n✅ No new draws found — already up to date.")
        else:
            print(f"\n🎉 Uploaded {new_uploaded} new draw(s).")

        return new_uploaded

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
                print("🛑 Selenium Chrome closed.")
            except Exception:
                pass
            self.driver = None


def main():
    creds_path = os.environ.get("FIREBASE_CREDENTIALS", "serviceAccountKey.json")
    start_draw_fallback = int(os.environ.get("START_DRAW_FALLBACK", "1"))
    max_consecutive_misses = int(os.environ.get("MAX_CONSECUTIVE_MISSES", "4"))
    max_new_draws = int(os.environ.get("MAX_NEW_DRAWS_PER_RUN", "60"))
    delay = float(os.environ.get("DRAW_DELAY_SECONDS", "1.0"))
    debug = os.environ.get("DEBUG", "0") == "1"

    if not os.path.exists(creds_path):
        print(f"❌ Credentials file not found: {creds_path}")
        raise SystemExit(1)

    scraper = None
    try:
        scraper = SubaDawasakScraper(creds_path, debug=debug)
        scraper.find_and_upload_new_draws(
            start_draw_fallback=start_draw_fallback,
            max_consecutive_misses=max_consecutive_misses,
            max_new_draws=max_new_draws,
            delay=delay,
        )
    finally:
        if scraper:
            scraper.close()


if __name__ == "__main__":
    main()
