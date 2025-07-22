"""
universal_scraper.py
--------------------
A plug-and-play Selenium scraper for any tabular (or row-based) web page.

Quick-start
-----------
1.  Fill in a CONFIG dict (see bottom of file or pass one at runtime).
2.  Choose / implement a StorageBackend (JSON, S3, SQL, etc.).
3.  Call run_scraper(CONFIG, storage_backend).

The core library code below never needs editing when you move to a
different site—only the CONFIG changes.
"""

import json, os, logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ───────────────────────────── Logging ─────────────────────────────
log = logging.getLogger("universal_scraper")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler())

# ──────────────── Driver factory (works local & AWS Lambda) ─────────
def create_driver(headless: bool = True) -> webdriver.Chrome:
    in_lambda = "LAMBDA_TASK_ROOT" in os.environ
    chrome_binary = "/opt/bin/chromium" if in_lambda else None  # let Chrome choose locally

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.binary_location = chrome_binary

    return webdriver.Chrome(service=Service(), options=opts)

# ─────────────── Storage backend plug-ins (choose / extend) ─────────
class StorageBackend(ABC):
    @abstractmethod
    def load(self) -> List[Dict[str, Any]]:
        ...
    @abstractmethod
    def save(self, data: List[Dict[str, Any]]) -> None:
        ...

class LocalJSON(StorageBackend):
    """Simple local file storage."""
    def __init__(self, path: str = "scrape_results.json", make_backup: bool = True):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix('.json.backup')
        self.make_backup = make_backup

    def load(self):
        if not os.path.exists(self.path):
            return []
        try: 
            with open(self.path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e: 
            log.error(f"Error loading {self.path}: {e}")
            if self.backup_path.exists() and self.make_backup: 
                log.info(f"Backup found...will attempt to load from backup {self.backup_path}")
                try: 
                    with open(self.backup_path, 'r') as f:
                        return json.load(f)
                except Exception as backupE: 
                    log.error(f"Failed to load from backup {self.backup_path}: {backupE}")
        except FileNotFoundError as e: 
            log.error(f"File {self.path} not found: {e}")
            if self.backup_path.exists() and self.make_backup: 
                log.info(f"Backup found...will attempt to load from backup {self.backup_path}")
                try: 
                    with open(self.backup_path, 'r') as f:
                        return json.load(f)
                except Exception as backupE: 
                    log.error(f"Failed to load from backup {self.backup_path}: {backupE}")
        except PermissionError as e: 
            log.error(f"Permissions error on  {self.path}: {e}")
            if self.backup_path.exists() and self.make_backup: 
                log.info(f"Backup found...will attempt to load from backup {self.backup_path}")
                try: 
                    with open(self.backup_path, 'r') as f:
                        return json.load(f)
                except Exception as backupE: 
                    log.error(f"Failed to load from backup {self.backup_path}: {backupE}")
        return []

    def save(self, data):
        if self.make_backup and self.path.exists(): 
            try: 
                self.backup_path.write_text(self.path.read_text())
                log.info(f"Created backup at {self.backup_path}")
            except Exception as e: 
                log.warning(f"Failed to create backup at {self.backup_path}")

        try:
            with open(self.path, 'w') as f:
                json.dump(data, f, indent=2)
                log.info(f"Saved {len(data)} records to {self.path}")
        except Exception as e:
            log.error(f"Failed to save data: {e}")

# ───────────────────────── Core scraping helper ────────────────────
def scrape_rows(driver: webdriver.Chrome,
                url: str,
                row_css: str,
                column_map: Dict[str, int],
                wait_sec: int = 30,
                max_retry: int = 5) -> List[Dict[str, str]]:
    """Navigate to *url*, wait for rows matching *row_css*, return parsed data.

    column_map = {"field_name": column_index_in_row, ...}
    """
    for retry in range(max_retry):
        try:
            log.info(f"Loading {url}, attempt: {retry+1}")
            driver.get(url)

            WebDriverWait(driver, wait_sec).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, row_css))
            )
            rows = driver.find_elements(By.CSS_SELECTOR, row_css)

            if not rows:
                log.warning(f"Did not find any rows with row_css: {row_css}")
                return []

            data: List[Dict[str, str]] = []
            num_cols= max(column_map.values()) + 1 if column_map else 0
            for r in rows:
                cells = r.find_elements(By.TAG_NAME, "td")
                if len(cells) < num_cols:
                    continue
                record = {field: cells[idx].text.strip() for field, idx in column_map.items()}
                data.append(record)
            log.info("Extracted %d rows", len(data))
            return data
        except TimeoutException:
            log.warning(f"Timeout waiting for rows on attempt {retry + 1}")
            if retry == max_retries - 1:
                log.error(f"Failed to load {url} after {max_retries} attempts")
                return []
        except Exception as e:
            log.error(f"Error on attempt {retry + 1}: {e}")
            if retry == max_retries - 1:
                raise
# ────────────────────────── Data merge helper ──────────────────────
def merge_unique(old: List[Dict[str, Any]],
                 new: List[Dict[str, Any]],
                 key_fields: List[str]) -> List[Dict[str, Any]]:
    """Return union of old+new where *key_fields* identify a unique row."""
    index = {tuple(o[k] for k in key_fields): i for i, o in enumerate(old)}
    for item in new:
        try: 
            k = tuple(item[k] for k in key_fields)
            if k in index:
                old[index[k]] = item          # overwrite old copy
            else:
                old.append(item)
        except Exception as e: 
            log.waring(f'Failed processing new record: {e}')
            continue 
    return old

# ─────────────────────────── Main worker ───────────────────────────
def run_scraper(config: Dict[str, Any], storage: StorageBackend) -> None:

    keyList = ["url", "row_css", "column_map"]
    missingKeys = [k for k in keyList if k not in config]
    if missingKeys: 
        raise ValueError(f"MUST INCLUDE ALL KEYS, MISSING KEYS: {missingKeys}")

    drv = None
    try:
        drv = create_driver()
        fresh = scrape_rows(drv,
                            url=config["url"],
                            row_css=config["row_css"],
                            column_map=config["column_map"])
        existing = storage.load()
        merged   = merge_unique(existing, fresh, config["key_fields"])
        storage.save(merged)
        log.info("Saved %d total records", len(merged))
    except Exception as e:
        log.error(f"Scrape failed: {e}")    
    finally:
        if drv:
            drv.quit()

# ─────────────────────────── Example stub ──────────────────────────
if __name__ == "__main__":
    CONFIG = {
        "url": "https://www.w3schools.com/html/html_tables.asp",
        "row_css": "#customers tr:not(:first-child)",  # skip header row
        "column_map": {
            "company": 0,
            "contact": 1,
            "country": 2
        },
        "key_fields": ["company"]
    }

    storage_backend = LocalJSON("example_results.json")
    run_scraper(CONFIG, storage_backend)

# ───────────────────────── AWS Lambda wrapper ──────────────────────
def lambda_handler(event, context):
    """
    Deploy this file to Lambda along with a headless Chrome layer.
    Supply CONFIG via env-vars, Secrets Manager, SSM, or code import.
    """
    CONFIG = {...}                 # ← provide the same structure
    storage = LocalJSON("/tmp/results.json")  # or custom backend
    run_scraper(CONFIG, storage)
    return {"statusCode": 200,
            "body": f"Scrape completed @ {datetime.utcnow().isoformat()}Z"}