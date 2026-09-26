"""Provider keys, read at runtime, held in memory, never written anywhere.

Two sources, chosen in the config file (which itself holds only secret NAMES):

  gcp  GCP Secret Manager via the `gcloud` CLI and the machine's own credentials.
  env  process environment (for a host with no GCP; still nothing on disk).

A key that cannot be read is reported by its NAME and the reason class only. No
value, prefix, suffix or length ever reaches a log, an error, or a response.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyStatus:
    provider: str
    source: str          # gcp | env
    name: str            # secret or variable name -- never the value
    present: bool
    detail: str          # "read" | "not configured" | "gcloud not installed" | ...


class Keyring:
    def __init__(self, source: str, names: dict[str, str], gcp_project: str | None = None):
        if source not in ("gcp", "env"):
            raise ValueError("secrets.source must be 'gcp' or 'env'")
        self.source = source
        self.names = dict(names)          # provider -> secret name / env var
        self.project = gcp_project
        self._vals: dict[str, str] = {}
        self._status: dict[str, KeyStatus] = {}
        self._lock = threading.Lock()

    def _read_gcp(self, name: str) -> tuple[str, str]:
        exe = shutil.which("gcloud")
        if not exe:
            return "", "gcloud not installed"
        if not self.project:
            return "", "secrets.gcp_project not set"
        try:
            r = subprocess.run(
                [exe, "secrets", "versions", "access", "latest", "--secret=" + name,
                 "--project=" + self.project],
                capture_output=True, text=True, timeout=60, shell=False)
        except subprocess.TimeoutExpired:
            return "", "gcloud timed out"
        except OSError:
            # Windows ships gcloud as a .cmd, which needs the shell to launch.
            r = subprocess.run(
                ["gcloud", "secrets", "versions", "access", "latest", "--secret=" + name,
                 "--project=" + self.project],
                capture_output=True, text=True, timeout=60, shell=True)
        if r.returncode != 0:
            err = (r.stderr or "").lower()
            if "not_found" in err or "not found" in err:
                return "", "secret not found"
            if "permission" in err or "denied" in err:
                return "", "permission denied"
            if "reauth" in err or "credentials" in err or "login" in err:
                return "", "gcloud not authenticated"
            return "", "gcloud exit %d" % r.returncode
        v = (r.stdout or "").strip()
        return (v, "read") if v else ("", "secret is empty")

    def load(self, provider: str, *, force: bool = False) -> KeyStatus:
        with self._lock:
            if not force and provider in self._status:
                return self._status[provider]
            name = self.names.get(provider, "")
            if not name:
                st = KeyStatus(provider, self.source, "", False, "not configured")
            elif self.source == "env":
                v = os.environ.get(name, "").strip()
                if v:
                    self._vals[provider] = v
                st = KeyStatus(provider, "env", name, bool(v), "read" if v else "variable unset")
            else:
                v, why = self._read_gcp(name)
                if v:
                    self._vals[provider] = v
                else:
                    self._vals.pop(provider, None)
                st = KeyStatus(provider, "gcp", name, bool(v), why)
            self._status[provider] = st
            return st

    def get(self, provider: str) -> str:
        """The key, or "" -- callers check `load(...).present` for the reason."""
        self.load(provider)
        return self._vals.get(provider, "")

    def status(self) -> list[KeyStatus]:
        return [self.load(p) for p in self.names]

    def scrub(self, text: str) -> str:
        """Remove any key value from a string before it leaves the process."""
        for v in self._vals.values():
            if v and v in text:
                text = text.replace(v, "***")
        return text
