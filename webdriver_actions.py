from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
import time 

FIREFOX_BINARY_CANDIDATES = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]


def find_firefox_binary() -> Optional[str]:
    for p in FIREFOX_BINARY_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def default_geckodriver_service(script_dir: Path) -> Service:
    """
    Utilise geckodriver.exe s'il est dans le dossier du script, sinon Service() (PATH).
    """
    gecko_path = script_dir / "geckodriver.exe"
    if gecko_path.exists():
        return Service(executable_path=str(gecko_path))
    return Service()


@dataclass
class CaptureResult:
    current_url: str
    html_path: Path
    url_path: Path


class BrowserSession:
    """
    Session Selenium Firefox (visible, non headless) pour gérer captcha/cookies.
    Ne gère PAS la queue, ni l'UI.
    """

    def __init__(self) -> None:
        self.driver: Optional[webdriver.Firefox] = None

    def is_open(self) -> bool:
        return self.driver is not None

    def open(self, *, script_dir: Path, page_load_timeout: int = 60) -> None:
        if self.driver is not None:
            return

        firefox_bin = find_firefox_binary()
        if firefox_bin is None:
            raise FileNotFoundError(
                "Firefox introuvable. Installe Firefox ou ajoute son chemin dans FIREFOX_BINARY_CANDIDATES."
            )

        service = default_geckodriver_service(script_dir)

        options = Options()
        options.binary_location = firefox_bin
        # Visible (pas headless)
        options.set_preference("dom.webnotifications.enabled", False)

        driver = webdriver.Firefox(service=service, options=options)
        driver.set_page_load_timeout(page_load_timeout)

        self.driver = driver

    def goto(self, url: str) -> None:
        if self.driver is None:
            raise RuntimeError("BrowserSession non ouvert.")
        self.driver.get(url)

    def capture_page_source(self, *, out_dir: Path, base_name: str) -> CaptureResult:
        if self.driver is None:
            raise RuntimeError("BrowserSession non ouvert.")

        out_dir.mkdir(parents=True, exist_ok=True)

        current_url = self.driver.current_url
        html = self.driver.page_source

        html_path = out_dir / f"{base_name}.html"
        url_path = out_dir / f"{base_name}.url.txt"

        html_path.write_text(html, encoding="utf-8")
        url_path.write_text(current_url, encoding="utf-8")

        return CaptureResult(current_url=current_url, html_path=html_path, url_path=url_path)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None
