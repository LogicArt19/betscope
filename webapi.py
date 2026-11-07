# -*- coding: utf-8 -*-
"""
Adaptateur pour l'appli web Cotescope : le même moteur que le logiciel, exécuté dans le
navigateur (Pyodide). Pas de threads ni de réseau côté Python : c'est la page qui va
chercher les données sur le serveur et qui les passe ici.
"""

import json
import time

import betscope as b

b.config_dir = lambda: "/data"


class WebApi(b.Api):
    def __init__(self):
        super().__init__(start_loop=False)
        self._web = True
        self._remote = True

    # le réseau est géré par la page
    def refresh(self):
        return "web"

    def _do_refresh(self):
        return None

    def check_updates(self):
        return {"update": None, "version": b.APP_VERSION}

    def install_update(self):
        return False

    def open_url(self, url):
        return True

    def activate(self, key):
        with self._lock:
            self._cfg["license"] = str(key or "").strip()
            self._cfg["demo"] = False
            b.save_config(self._cfg)
            self._license_state = "ok"
        return {"ok": True}

    def deactivate(self):
        with self._lock:
            self._cfg["license"] = ""
            b.save_config(self._cfg)
        return True

    def demo(self):
        with self._lock:
            self._events, self._is_demo, self._last_fetch = b.demo_events(), True, time.time()
        self._reanalyze()
        return True

    def settle_now(self):
        """Règle l'historique avec les scores fournis par le serveur."""
        res = self._hist.data.get("results", {})

        def from_snapshot(slug, day):
            c = res.get(f"{slug}|{day}")
            if c is None:
                raise RuntimeError("score pas encore disponible")
            return c.get("v", [])
        self._hist._results_for = from_snapshot
        self._hist.settle_all(b.statsmod.ESPN_SLUGS)
        return True

    def tick(self):
        """Appelé par la page toutes les minutes : retire les matchs commencés, règle l'historique."""
        self._reanalyze()
        try:
            self.settle_now()
        except Exception:
            pass
        return True


API = None


def boot():
    global API
    API = WebApi()
    return True


def call(name, args_json):
    """Point d'entrée unique depuis JavaScript : renvoie toujours du JSON."""
    if name.startswith("_") or not hasattr(API, name):
        return json.dumps({"error": "méthode inconnue"})
    args = json.loads(args_json or "[]")
    try:
        return json.dumps(getattr(API, name)(*args), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def load_snapshot(snap_json):
    API.load_snapshot(json.loads(snap_json))
    return True
