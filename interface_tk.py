from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Optional, List

import tkinter as tk
from tkinter import ttk, messagebox

from webdriver_actions import BrowserSession


# ======== Statuts ========
STATUS_QUEUED = "Dans la queue"
STATUS_NAV = "Navigation…"
STATUS_DONE = "Recherche effectuée"
STATUS_CAPTURED = "Page récupérée"
STATUS_PAUSED = "Pause (captcha/cookies)"
STATUS_ERROR = "Erreur"


def detect_site(url: str) -> str:
    u = url.lower()
    if "morningstar." in u:
        return "morningstar"
    if "quantalys." in u:
        return "quantalys"
    return "autre"


def slugify(s: str, max_len: int = 60) -> str:
    s = s.strip().lower()
    s = re.sub(r"https?://", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] if len(s) > max_len else s


@dataclass
class Task:
    id: int
    url: str
    site: str = field(init=False)
    status: str = field(default=STATUS_QUEUED)
    saved_path: str = field(default="")
    error: str = field(default="")

    def __post_init__(self) -> None:
        self.site = detect_site(self.url)


class AcquisitionManager:
    """
    Logique (hors UI) :
      - queue des tâches
      - worker Selenium (thread)
      - pause / reprendre / recommencer
      - MAJ UI via callbacks
    """

    def __init__(self, *, on_task_update, on_log, on_state) -> None:
        self.on_task_update = on_task_update  # (task: Task) -> None
        self.on_log = on_log                  # (msg: str) -> None
        self.on_state = on_state              # (state_msg: str) -> None

        self.session = BrowserSession()

        self.tasks_by_id: Dict[int, Task] = {}
        self.queue: Deque[int] = deque()
        self._next_id = 1

        self._worker_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # flags de contrôle
        self._running = threading.Event()
        self._paused = threading.Event()

        # Tâche en cours + dernier succès (pour "Recommencer")
        self._current_task_id: Optional[int] = None
        self._last_done_task_id: Optional[int] = None

        # important : Reprendre doit redémarrer la tâche en cours
        self._restart_current_requested = threading.Event()

        # petit délai après navigation (stabilisation DOM) avant capture
        self.auto_capture_delay_s = 1.5

    # ---------- Queue ----------
    def add_urls(self, urls: List[str]) -> List[int]:
        ids: List[int] = []
        for raw in urls:
            url = raw.strip()
            if not url:
                continue
            tid = self._next_id
            self._next_id += 1
            task = Task(id=tid, url=url)
            self.tasks_by_id[tid] = task
            self.queue.append(tid)
            ids.append(tid)
            self.on_task_update(task)
        return ids

    def requeue_last_done_to_front(self) -> Optional[int]:
        if self._last_done_task_id is None:
            return None
        last = self.tasks_by_id.get(self._last_done_task_id)
        if last is None:
            return None

        tid = self._next_id
        self._next_id += 1
        task = Task(id=tid, url=last.url)
        self.tasks_by_id[tid] = task
        self.queue.appendleft(tid)
        self.on_task_update(task)
        self.on_log(f"Recommencer: remis en haut de pile → {task.url}")
        return tid

    # ---------- Session ----------
    def open_browser_if_needed(self) -> None:
        if self.session.is_open():
            return
        script_dir = Path(__file__).resolve().parent
        self.session.open(script_dir=script_dir)
        self.on_state("Firefox ouvert (session unique conservée jusqu’à fermeture de l’UI).")

    # ---------- Contrôles ----------
    def start(self) -> None:
        self.open_browser_if_needed()
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

        self._paused.clear()
        self._running.set()
        self.on_state("Acquisition démarrée (auto).")

    def pause(self) -> None:
        # On met en pause : le navigateur reste ouvert.
        self._paused.set()
        self._running.clear()

        # MAJ statut de la tâche en cours (si connue)
        if self._current_task_id is not None:
            t = self.tasks_by_id.get(self._current_task_id)
            if t and t.status not in (STATUS_CAPTURED, STATUS_ERROR):
                t.status = STATUS_PAUSED
                self.on_task_update(t)

        self.on_state("PAUSE : résous captcha/cookies, puis clique Reprendre.")

    def resume_restart_current(self) -> None:
        """
        Reprendre = recommencer l'acquisition en cours :
        - re-GET + re-capture sur la tâche courante
        - puis enchaîner sur la suite
        """
        self._restart_current_requested.set()
        self._paused.clear()
        self._running.set()
        self.on_state("Reprise : redémarrage de l’acquisition en cours…")

    def stop(self) -> None:
        self._stop.set()
        self._running.clear()
        self._paused.clear()

    def close_browser(self) -> None:
        self.session.close()

    # ---------- Worker ----------
    def _restart_current_if_needed(self) -> None:
        """
        Si l'utilisateur a cliqué "Reprendre", on remet la tâche courante en tête
        (sans créer de nouvelle tâche), en réinitialisant son état.
        """
        if not self._restart_current_requested.is_set():
            return

        self._restart_current_requested.clear()

        tid = self._current_task_id
        if tid is None:
            return

        task = self.tasks_by_id.get(tid)
        if task is None:
            return

        # Si déjà capturée/erreur, pas besoin
        if task.status in (STATUS_CAPTURED, STATUS_ERROR):
            return

        # Réinitialise et remet en tête
        task.status = STATUS_QUEUED
        task.saved_path = ""
        task.error = ""
        self.on_task_update(task)

        # Important : si jamais elle était déjà dans la queue, éviter doublon
        try:
            self.queue.remove(tid)
        except ValueError:
            pass

        self.queue.appendleft(tid)
        self.on_log(f"Reprendre → tâche en cours remise en tête : {task.url}")

        # On "libère" la courante : la prochaine itération la reprendra proprement
        self._current_task_id = None

    def _worker_loop(self) -> None:
        self.on_log("Worker Selenium démarré.")

        while not self._stop.is_set():
            # Attendre un start
            if not self._running.is_set():
                time.sleep(0.1)
                continue

            # Si pause : attendre (et permettre reprise)
            if self._paused.is_set():
                # Au retour de pause, on doit redémarrer l'acquisition en cours
                self._restart_current_if_needed()
                time.sleep(0.1)
                continue

            # Si reprise demandée : on prépare la requeue immédiate
            self._restart_current_if_needed()

            # Rien à faire ?
            if not self.queue:
                self.on_state("Queue vide. (Firefox reste ouvert)")
                self._running.clear()
                time.sleep(0.1)
                continue

            tid = self.queue.popleft()
            task = self.tasks_by_id.get(tid)
            if task is None:
                continue

            self._current_task_id = tid

            # ----- NAVIGATION -----
            task.status = STATUS_NAV
            task.error = ""
            self.on_task_update(task)

            try:
                self.on_log(f"GET → {task.url}")
                self.session.goto(task.url)

                # Si pause a été cliquée pendant le chargement (elle sera vue ici)
                if self._paused.is_set():
                    task.status = STATUS_PAUSED
                    self.on_task_update(task)
                    continue

                task.status = STATUS_DONE
                self.on_task_update(task)
                self.on_state(f"Recherche effectuée : {task.site}")

                # ----- ATTENTE éventuelle post-load (stabilisation DOM) -----
                t0 = time.time()
                while (time.time() - t0) < self.auto_capture_delay_s:
                    if self._stop.is_set() or self._paused.is_set():
                        break
                    time.sleep(0.05)

                if self._stop.is_set():
                    break

                # Si pause pendant la stabilisation : on attend, puis "Reprendre" relancera la tâche
                if self._paused.is_set():
                    task.status = STATUS_PAUSED
                    self.on_task_update(task)
                    continue

                # ----- CAPTURE -----
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                base = f"{tid:04d}_{task.site}_{ts}_{slugify(task.url)}"
                out_dir = Path("captures")
                time.sleep(1.0)  # petite attente avant capture
                result = self.session.capture_page_source(out_dir=out_dir, base_name=base)

                task.status = STATUS_CAPTURED
                task.saved_path = str(result.html_path)
                self.on_task_update(task)
                self.on_state(f"Page récupérée → {result.html_path}")
                self._last_done_task_id = tid

                # IMPORTANT: on enchaîne automatiquement → prochaine URL

            except Exception as e:
                task.status = STATUS_ERROR
                task.error = f"{type(e).__name__}: {e}"
                self.on_task_update(task)
                self.on_state("Erreur sur une tâche (voir colonne Erreur).")
                self.on_log(task.error)

            finally:
                # On conserve current_task_id tant que la tâche n’est pas terminée,
                # car "Reprendre" doit relancer celle-ci si elle est en pause.
                if task.status in (STATUS_CAPTURED, STATUS_ERROR):
                    self._current_task_id = None

        self.on_log("Worker Selenium arrêté.")


# ======== UI Tkinter ========
class StockSentinelUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("StockSentinel - Acquisition multi-sites (auto)")
        self.root.geometry("1150x640")

        self.state_var = tk.StringVar(value="Prêt.")

        self.manager = AcquisitionManager(
            on_task_update=self._ui_task_update_safe,
            on_log=self._ui_log_safe,
            on_state=self._ui_state_safe,
        )

        self._build_ui()
        self._seed_default_urls()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 8}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, **pad)

        # --- Controls ---
        ctrl = ttk.Frame(main)
        ctrl.pack(fill="x")

        ttk.Button(ctrl, text="Ouvrir Firefox", command=self.open_browser).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Démarrer", command=self.start).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Pause", command=self.pause).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Reprendre (recommence en cours)", command=self.resume).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Recommencer (dernier)", command=self.retry_last).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Quitter", command=self.quit_all).pack(side="right", padx=6)

        # --- Middle split ---
        mid = ttk.PanedWindow(main, orient="horizontal")
        mid.pack(fill="both", expand=True)

        left = ttk.Frame(mid)
        right = ttk.Frame(mid)
        mid.add(left, weight=3)
        mid.add(right, weight=2)

        # Left: tasks list
        ttk.Label(left, text="File des recherches (URL) + statuts").pack(anchor="w")

        cols = ("id", "site", "status", "saved", "error", "url")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=18)
        self.tree.heading("id", text="#")
        self.tree.heading("site", text="Site")
        self.tree.heading("status", text="Statut")
        self.tree.heading("saved", text="Fichier HTML")
        self.tree.heading("error", text="Erreur")
        self.tree.heading("url", text="URL")

        self.tree.column("id", width=50, anchor="center")
        self.tree.column("site", width=110, anchor="w")
        self.tree.column("status", width=170, anchor="w")
        self.tree.column("saved", width=240, anchor="w")
        self.tree.column("error", width=240, anchor="w")
        self.tree.column("url", width=560, anchor="w")

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Right: URL input
        ttk.Label(right, text="Entrée : liste d'URLs à visiter (une par ligne)").pack(anchor="w")
        self.txt_urls = tk.Text(right, height=16, wrap="none")
        self.txt_urls.pack(fill="both", expand=True)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Ajouter à la queue", command=self.add_from_text).pack(side="left", padx=6)
        ttk.Button(btns, text="Vider le champ", command=lambda: self._set_urls_text("")).pack(side="left", padx=6)

        # Logs
        ttk.Label(main, text="Logs").pack(anchor="w")
        self.txt_log = tk.Text(main, height=8, wrap="word")
        self.txt_log.pack(fill="x", expand=False)

        # Status bar
        bar = ttk.Frame(main)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text="Statut :").pack(side="left")
        ttk.Label(bar, textvariable=self.state_var).pack(side="left", padx=8)

    def _seed_default_urls(self) -> None:
        defaults = [
            "https://global.morningstar.com/fr/investissements/fonds/0P0000K538/cours",
            "https://www.quantalys.com/Fonds/62775",
            "https://global.morningstar.com/fr/investissements/actions/0P00013FW3/cours",
        ]
        self._set_urls_text("\n".join(defaults))

    def _set_urls_text(self, s: str) -> None:
        self.txt_urls.delete("1.0", "end")
        self.txt_urls.insert("1.0", s)

    # ---- Thread-safe wrappers ----
    def _ui_task_update_safe(self, task: Task) -> None:
        self.root.after(0, lambda: self._ui_task_update(task))

    def _ui_log_safe(self, msg: str) -> None:
        self.root.after(0, lambda: self._ui_log(msg))

    def _ui_state_safe(self, msg: str) -> None:
        self.root.after(0, lambda: self.state_var.set(msg))

    # ---- UI update ----
    def _ui_task_update(self, task: Task) -> None:
        iid = str(task.id)
        values = (task.id, task.site, task.status, task.saved_path, task.error, task.url)
        if self.tree.exists(iid):
            self.tree.item(iid, values=values)
        else:
            self.tree.insert("", "end", iid=iid, values=values)

    def _ui_log(self, msg: str) -> None:
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    # ---- Buttons ----
    def open_browser(self) -> None:
        try:
            self.manager.open_browser_if_needed()
        except Exception as e:
            messagebox.showerror("Erreur ouverture Firefox", f"{type(e).__name__}: {e}")

    def add_from_text(self) -> None:
        raw = self.txt_urls.get("1.0", "end").splitlines()
        urls = [u.strip() for u in raw if u.strip()]
        if not urls:
            messagebox.showinfo("Info", "Aucune URL à ajouter.")
            return
        ids = self.manager.add_urls(urls)
        self._ui_log(f"Ajouté {len(ids)} URL(s) à la queue.")

    def start(self) -> None:
        try:
            self.manager.start()
        except Exception as e:
            messagebox.showerror("Erreur", f"{type(e).__name__}: {e}")

    def pause(self) -> None:
        self.manager.pause()

    def resume(self) -> None:
        self.manager.resume_restart_current()

    def retry_last(self) -> None:
        tid = self.manager.requeue_last_done_to_front()
        if tid is None:
            messagebox.showinfo("Info", "Aucune tâche précédente à recommencer.")
        else:
            self._ui_log(f"Tâche {tid} ajoutée en tête (recommencer).")

    def quit_all(self) -> None:
        # IMPORTANT: fermeture webdriver uniquement ici
        try:
            self.manager.stop()
            self.manager.close_browser()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = StockSentinelUI(root)
    root.protocol("WM_DELETE_WINDOW", app.quit_all)
    root.mainloop()


if __name__ == "__main__":
    main()
