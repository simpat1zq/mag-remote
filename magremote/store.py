"""Tiny JSON-file store for configured boxes (lives in the data volume)."""
from __future__ import annotations

import json
import os
import threading
import uuid

SECRET_FIELDS = ("password", "code")


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._data = self._load()
        if not self._data.get("dev_id"):
            # identity this install presents to boxes that use pairing
            self._data["dev_id"] = str(uuid.uuid4())
            self._save()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except ValueError as e:
            raise SystemExit(f"{self.path} is not valid JSON: {e}")
        data.setdefault("boxes", [])
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def dev_id(self):
        return self._data["dev_id"]

    def boxes(self):
        with self._lock:
            return [dict(b) for b in self._data["boxes"]]

    def get(self, box_id):
        with self._lock:
            for b in self._data["boxes"]:
                if b["id"] == box_id:
                    return dict(b)
        return None

    def add(self, box):
        with self._lock:
            # re-adding the same box replaces it (e.g. after re-pairing)
            self._data["boxes"] = [b for b in self._data["boxes"]
                                   if not (b["host"] == box["host"] and b["port"] == box["port"])]
            box = {"id": uuid.uuid4().hex[:10], **box}
            self._data["boxes"].append(box)
            self._save()
            return dict(box)

    def update(self, box_id, **fields):
        with self._lock:
            for b in self._data["boxes"]:
                if b["id"] == box_id:
                    b.update({k: v for k, v in fields.items() if v is not None})
                    self._save()
                    return dict(b)
        return None

    def remove(self, box_id):
        with self._lock:
            before = len(self._data["boxes"])
            self._data["boxes"] = [b for b in self._data["boxes"] if b["id"] != box_id]
            self._save()
            return len(self._data["boxes"]) != before

    @staticmethod
    def public(box):
        return {k: v for k, v in box.items() if k not in SECRET_FIELDS}
