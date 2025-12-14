from __future__ import annotations

import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options

# ====== CONFIG À AJUSTER SI BESOIN ======
TARGET_URL = "https://global.morningstar.com/fr/investissements/fonds/0P0000K538/cours"

# Chemins possibles de Firefox sur Windows (le script choisit le premier qui existe)
FIREFOX_BINARY_CANDIDATES = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]
# =======================================


def find_firefox_binary() -> str | None:
    """Retourne le chemin de firefox.exe si trouvé, sinon None."""
    for p in FIREFOX_BINARY_CANDIDATES:
        if Path(p).exists():
            return p
    return None


class StockSentinelApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Stock Sentinel - Capture Morningstar (manuel puis capture)")
        self.root.geometry("800x420")

        self.driver: webdriver.Firefox | None = None
        self.status_var = tk.StringVar(value="Prêt. Clique « 1) Ouvrir le navigateur ».")

        self._build_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 8}

        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True, **pad)

        ttk.Label(frm, text="URL cible :").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=TARGET_URL)
        ttk.Entry(frm, textvariable=self.url_var, width=100).grid(row=1, column=0, columnspan=3, sticky="we")

        self.btn_open = ttk.Button(frm, text="1) Ouvrir Firefox (non-headless)", command=self.open_browser)
        self.btn_open.grid(row=2, column=0, sticky="we", **pad)

        self.btn_capture = ttk.Button(frm, text="2) Je suis prêt → Capturer la page affichée", command=self.capture_page)
        self.btn_capture.grid(row=2, column=1, sticky="we", **pad)
        self.btn_capture.state(["disabled"])

        self.btn_quit = ttk.Button(frm, text="Quitter (ferme aussi Firefox)", command=self.quit_all)
        self.btn_quit.grid(row=2, column=2, sticky="we", **pad)

        info = (
            "Mode d’emploi :\n"
            "1) Ouvre Firefox via le bouton.\n"
            "2) Dans Firefox, fais manuellement : cookies, conditions, login, CAPTCHA, etc.\n"
            "3) Quand la page voulue est affichée, clique « Capturer ».\n\n"
            "Sorties :\n"
            "- captures/morningstar_capture.html (HTML rendu)\n"
            "- captures/morningstar_capture_url.txt (URL courante)\n\n"
            "Note : ce script force le chemin de Firefox si installé dans Program Files.\n"
        )
        self.text = tk.Text(frm, height=11, wrap="word")
        self.text.insert("1.0", info)
        self.text.configure(state="disabled")
        self.text.grid(row=3, column=0, columnspan=3, sticky="nsew", **pad)

        ttk.Label(frm, text="Statut :").grid(row=4, column=0, sticky="w")
        ttk.Label(frm, textvariable=self.status_var).grid(row=4, column=1, columnspan=2, sticky="w")

        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)
        frm.rowconfigure(3, weight=1)

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)
        self.root.update_idletasks()

    def open_browser(self) -> None:
        if self.driver is not None:
            messagebox.showinfo("Info", "Firefox est déjà ouvert.")
            return

        self.btn_open.state(["disabled"])
        self._set_status("Ouverture de Firefox via GeckoDriver...")

        def _worker():
            try:
                script_dir = Path(__file__).resolve().parent
                gecko_path = script_dir / "geckodriver.exe"

                if gecko_path.exists():
                    service = Service(executable_path=str(gecko_path))
                else:
                    service = Service()  # nécessite geckodriver dans PATH

                options = Options()
                options.set_preference("dom.webnotifications.enabled", False)

                # IMPORTANT : forcer le chemin du binaire Firefox si trouvé
                firefox_bin = find_firefox_binary()
                if firefox_bin is None:
                    raise FileNotFoundError(
                        "Firefox introuvable. Installe Firefox ou ajoute son chemin dans FIREFOX_BINARY_CANDIDATES."
                    )
                options.binary_location = firefox_bin

                driver = webdriver.Firefox(service=service, options=options)
                driver.set_page_load_timeout(60)

                url = self.url_var.get().strip()
                driver.get(url)

                self.driver = driver
                self.root.after(0, lambda url=url, firefox_bin=firefox_bin: self._after_browser_open(url, firefox_bin))

            except Exception as e:
                self.root.after(0, lambda e=e: self._on_browser_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _after_browser_open(self, url: str, firefox_bin: str) -> None:
        self._set_status(
            f"Firefox ouvert (binaire: {firefox_bin}). Fais tes actions manuelles puis clique « Capturer »."
        )
        self.btn_capture.state(["!disabled"])

    def _on_browser_error(self, e: Exception) -> None:
        self.btn_open.state(["!disabled"])
        self._set_status("Erreur à l’ouverture du navigateur.")
        messagebox.showerror(
            "Erreur Selenium/Firefox",
            f"{type(e).__name__}: {e}\n\n"
            "Actions :\n"
            "- Vérifie que Firefox est installé.\n"
            "- Si Firefox est ailleurs, ajoute le chemin exact de firefox.exe dans FIREFOX_BINARY_CANDIDATES.\n"
            "- Vérifie geckodriver.exe dans le dossier du script.\n"
            f"Dossier script : {Path(__file__).resolve().parent}\n"
        )

    def capture_page(self) -> None:
        if self.driver is None:
            messagebox.showwarning("Attention", "Firefox n’est pas ouvert.")
            return

        self.btn_capture.state(["disabled"])
        self._set_status("Capture en cours (HTML rendu)...")

        def _worker():
            try:
                driver = self.driver
                if driver is None:
                    raise RuntimeError("Driver non initialisé.")

                time.sleep(1.5)  # stabilisation légère

                current_url = driver.current_url
                html = driver.page_source

                out_dir = Path("captures")
                out_dir.mkdir(exist_ok=True)

                out_html = out_dir / "morningstar_capture.html"
                out_url = out_dir / "morningstar_capture_url.txt"

                out_html.write_text(html, encoding="utf-8")
                out_url.write_text(current_url, encoding="utf-8")

                self.root.after(0, lambda out_html=out_html, out_url=out_url: self._after_capture(out_html, out_url))

            except Exception as e:
                self.root.after(0, lambda e=e: self._on_capture_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _after_capture(self, out_html: Path, out_url: Path) -> None:
        self._set_status("Capture terminée.")
        messagebox.showinfo(
            "OK",
            "Fichiers enregistrés :\n"
            f"- HTML : {out_html.resolve()}\n"
            f"- URL : {out_url.resolve()}\n"
        )
        self.btn_capture.state(["!disabled"])

    def _on_capture_error(self, e: Exception) -> None:
        self._set_status("Erreur pendant la capture.")
        messagebox.showerror("Erreur capture", f"{type(e).__name__}: {e}")
        self.btn_capture.state(["!disabled"])

    def quit_all(self) -> None:
        try:
            if self.driver is not None:
                self.driver.quit()
                self.driver = None
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = StockSentinelApp(root)
    root.protocol("WM_DELETE_WINDOW", app.quit_all)
    root.mainloop()


if __name__ == "__main__":
    main()
