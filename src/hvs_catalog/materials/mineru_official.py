"""Submit fixed PDFs in native cloud batches and retain unchanged official ZIPs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import time
import zipfile
from pathlib import Path

import requests

from ..records.common import ArtifactRef, canonical_json, sha256_of
from .mineru import MAX_OUTPUT_BYTES, MinerUError, read_result

API_URL = "https://mineru.net/api/v4"
MODEL = "vlm"


class _RateLimitRejection(MinerUError):
    """A received HTTP 429 confirms that no batch was created."""


class OfficialMinerU:
    def __init__(self, space, *, api_key_file=None, env_file=None):
        self.space = space
        self.api_key_file = api_key_file
        self.env_file = env_file
        self.api_url = API_URL
        self.poll_interval = 5
        self.timeout_seconds = 3600
        self._results = {}

    def fingerprint(self):
        return {
            "name": "MinerU official API",
            "version": "v4",
            "adapter": "native-cloud-1",
            "api_url": self.api_url,
            "model_version": MODEL,
            "pages": "all",
            "is_ocr": False,
            "enable_formula": True,
            "enable_table": True,
            "language": "en",
        }

    def _token(self):
        values = {}
        if self.env_file is not None:
            for line in Path(self.env_file).read_text().splitlines():
                match = re.match(
                    r"^\s*(?:export\s+)?(MINERU_TOKEN|MINERU_API_KEY)\s*=\s*(.*)$", line
                )
                if match:
                    parts = shlex.split(match[2], comments=True)
                    if len(parts) != 1:
                        raise MinerUError("Invalid MinerU credential file")
                    values[match[1]] = parts[0]
        token = (
            Path(self.api_key_file).read_text().strip()
            if self.api_key_file is not None
            else os.environ.get("MINERU_TOKEN")
            or os.environ.get("MINERU_API_KEY")
            or values.get("MINERU_TOKEN")
            or values.get("MINERU_API_KEY")
            or ""
        )
        if not token or any(c.isspace() for c in token):
            raise MinerUError("Configure MINERU_TOKEN or a MinerU credential file")
        return token

    @staticmethod
    def _body(response):
        parts, size = [], 0
        for part in response.iter_content(65536):
            size += len(part)
            if size > MAX_OUTPUT_BYTES:
                raise MinerUError("MinerU response exceeds size limits")
            parts.append(part)
        return b"".join(parts)

    def _api(self, method, path, token, *, rate_limit_retry=True, **kwargs):
        try:
            with requests.request(
                method,
                self.api_url + path,
                headers={"Authorization": "Bearer " + token},
                timeout=(30, 120),
                allow_redirects=False,
                stream=True,
                **kwargs,
            ) as response:
                if response.status_code == 429:
                    if rate_limit_retry:
                        response.close()
                        time.sleep(60)
                        return self._api(
                            method, path, token, rate_limit_retry=False, **kwargs
                        )
                    raise _RateLimitRejection("MinerU API HTTP 429")
                if not 200 <= response.status_code < 300:
                    raise MinerUError(f"MinerU API HTTP {response.status_code}")
                result = json.loads(self._body(response))
        except requests.RequestException:
            raise MinerUError("MinerU API network request failed") from None
        except MinerUError:
            raise
        except ValueError:
            raise MinerUError("MinerU API returned invalid JSON") from None
        if not isinstance(result, dict) or result.get("code") != 0:
            # Service messages can contain credentials or signed URLs.
            raise MinerUError("MinerU API rejected request")
        if not isinstance(result.get("data"), dict):
            raise MinerUError("MinerU API response data is missing")
        return result["data"]

    def _transfer(self, method, url, *, payload=None):
        try:
            with requests.request(
                method,
                url,
                # A bytes body becomes one socket write; stream bounded chunks instead.
                data=io.BytesIO(payload) if payload is not None else None,
                # The socket keeps this timeout while writing a streamed request body.
                timeout=(120 if method == "PUT" else 30, 120),
                allow_redirects=False,
                stream=True,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise MinerUError(
                        f"MinerU file transfer HTTP {response.status_code}"
                    )
                return self._body(response) if method == "GET" else b""
        except requests.RequestException as exc:
            # Exception messages may contain signed URLs; expose only their type.
            raise MinerUError(
                f"MinerU file transfer failed ({method}, {type(exc).__name__})"
            ) from None

    def prepare_batch(self, inputs):
        """Respect the cloud's 50-file/minute limit; retain independent paper failures."""
        pending = list(
            dict.fromkeys((Path(path).resolve(), pages) for path, pages in inputs)
        )
        if not pending:
            return
        token = self._token()
        fresh, known = [], {}
        for path, pages in pending:
            saved = self.space.store().get_meta(self._input_key(path, pages))
            if saved:
                state = json.loads(self.space.store().get_meta(saved))
                if state.get("batch_id") or state.get("creation_started"):
                    known[saved] = state
                else:
                    # A confirmed rejected creation may be regrouped without new tasks.
                    fresh.append((path, pages))
            else:
                fresh.append((path, pages))
        chunks = [
            [
                (self.space.root / entry["path"], entry["pages"])
                for entry in state["files"]
            ]
            for state in known.values()
        ]
        chunks.extend(fresh[start : start + 50] for start in range(0, len(fresh), 50))
        # Upload all groups before waiting on any queue or downloading results.
        for stage_only in (True, False):
            for chunk in chunks:
                if all(path in self._results for path, _ in chunk):
                    continue
                try:
                    self._prepare_chunk(chunk, token, stage_only=stage_only)
                except MinerUError as exc:
                    for path, _ in chunk:
                        self._results.setdefault(path, exc)
                except (KeyError, TypeError, ValueError):
                    for path, _ in chunk:
                        self._results.setdefault(
                            path, MinerUError("Invalid MinerU cloud response")
                        )

    def _input_path(self, path):
        return (
            str(path.relative_to(self.space.root))
            if path.is_relative_to(self.space.root)
            else str(path)
        )

    def _input_key(self, path, pages):
        return "mineru-cloud-input:" + sha256_of(
            [
                self._input_path(path),
                pages,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                self.fingerprint(),
            ]
        )

    def _prepare_chunk(self, chunk, token, *, stage_only=False):
        entries = []
        for path, pages in chunk:
            payload = path.read_bytes()
            if not 1 <= pages <= 600 or len(payload) > 200 * 1024 * 1024:
                raise MinerUError("Fixed PDF exceeds official cloud limits")
            entries.append(
                {
                    "data_id": hashlib.sha256(payload).hexdigest(),
                    "pages": pages,
                    "path": self._input_path(path),
                }
            )
        # Distinct papers may have identical PDF bytes. Keep each upload identity unique.
        for index, entry in enumerate(entries):
            entry["data_id"] += f"-{index}"
        key = "mineru-cloud-batch:" + sha256_of([entries, self.fingerprint()])
        saved = self.space.store().get_meta(key)
        state = json.loads(saved) if saved else {"files": entries}

        def save():
            self.space.store().set_meta(key, canonical_json(state))

        if "batch_id" not in state:
            if state.get("creation_started"):
                raise MinerUError(
                    "MinerU batch creation outcome unknown; do not resubmit"
                )
            state["creation_started"] = True
            save()
            for path, pages in chunk:
                self.space.store().set_meta(self._input_key(path, pages), key)
            try:
                response = self._api(
                    "POST",
                    "/file-urls/batch",
                    token,
                    json={
                        "files": [
                            {
                                "name": entry["data_id"] + ".pdf",
                                "data_id": entry["data_id"],
                                "is_ocr": False,
                            }
                            for entry in entries
                        ],
                        "model_version": MODEL,
                        "enable_formula": True,
                        "enable_table": True,
                        "language": "en",
                    },
                )
            except _RateLimitRejection:
                state.pop("creation_started")
                save()
                raise
            if len(response.get("file_urls", [])) != len(entries) or not response.get(
                "batch_id"
            ):
                raise MinerUError("MinerU batch upload identities are missing")
            state.update(
                batch_id=response["batch_id"],
                upload_urls=response["file_urls"],
                uploaded=[],
            )
            save()
        confirmed = state.setdefault("upload_confirmed", [])
        uncertain = set(state["uploaded"]) - set(confirmed)
        if uncertain:
            # Legacy "uploaded" records mean attempted, not acknowledged. A known
            # remote task decides whether a missing upload may be sent again.
            rows = self._batch_rows(state, token)
            for index in uncertain:
                if rows[entries[index]["data_id"]]["state"] != "waiting-file":
                    confirmed.append(index)
            save()
        for index, (path, _) in enumerate(chunk):
            if path in self._results or index in confirmed:
                continue
            payload = path.read_bytes()
            if (
                hashlib.sha256(payload).hexdigest()
                != entries[index]["data_id"].rsplit("-", 1)[0]
            ):
                raise MinerUError("Fixed PDF changed before upload")
            if index not in state["uploaded"]:
                state["uploaded"].append(index)
                save()
            try:
                self._transfer("PUT", state["upload_urls"][index], payload=payload)
            except MinerUError as exc:
                # One interrupted file must not prevent other uploads or results.
                self._results[path] = exc
                continue
            confirmed.append(index)
            save()
        if stage_only:
            return
        active = [
            entry["data_id"]
            for (path, _), entry in zip(chunk, entries, strict=True)
            if path not in self._results
        ]
        if not active:
            return
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            rows = self._batch_rows(state, token)
            if all(rows[data_id]["state"] in {"done", "failed"} for data_id in active):
                break
            if time.monotonic() >= deadline:
                raise MinerUError(
                    "MinerU batch polling timed out; resume the saved batch"
                )
            time.sleep(self.poll_interval)
        for (path, pages), entry in zip(chunk, entries, strict=True):
            if path in self._results:
                continue
            row = rows[entry["data_id"]]
            try:
                if row["state"] == "failed":
                    raise MinerUError("MinerU cloud parsing failed")
                cache = state.setdefault("results", {}).get(entry["data_id"])
                if cache and (self.space.root / cache["path"]).is_file():
                    raw = self.space.read_file(ArtifactRef.model_validate(cache))
                else:
                    raw = self._transfer("GET", row["full_zip_url"])
                try:
                    parsed = read_result(raw, pages)
                except (
                    MinerUError,
                    ValueError,
                    KeyError,
                    TypeError,
                    zipfile.BadZipFile,
                ) as exc:
                    raise MinerUError(
                        "Unreadable official MinerU output",
                        artifacts={"mineru.zip": raw},
                    ) from exc
                if not cache or not (self.space.root / cache["path"]).is_file():
                    destination = (
                        self.space.paper_path(path.parent.name, "mineru/result.zip")
                        if self.space.is_literature
                        and path.parent.parent == self.space.root / "papers"
                        and path.name == "paper.pdf"
                        else "sources/mineru-cloud/"
                        + hashlib.sha256(raw).hexdigest()
                        + ".zip"
                    )
                    ref = self.space.publish_file(destination, raw)
                    state.setdefault("results", {})[entry["data_id"]] = ref.model_dump(
                        mode="json"
                    )
                    save()
                self._results[path] = (raw, parsed)
            except (MinerUError, KeyError) as exc:
                self._results[path] = exc

    def _batch_rows(self, state, token):
        response = self._api(
            "GET", "/extract-results/batch/" + state["batch_id"], token
        )
        entries = response.get("extract_result", [])
        rows = {row["data_id"]: row for row in entries}
        if (
            len(entries) != len(state["files"])
            or len(rows) != len(entries)
            or set(rows) != {entry["data_id"] for entry in state["files"]}
        ):
            raise MinerUError("MinerU returned inconsistent batch identities")
        if any(
            row["state"]
            not in {
                "done",
                "failed",
                "waiting-file",
                "pending",
                "running",
                "converting",
            }
            for row in rows.values()
        ):
            raise MinerUError("MinerU returned an unknown state")
        return rows

    def parse(self, path, page_count):
        path = Path(path).resolve()
        if path not in self._results:
            self.prepare_batch([(path, page_count)])
        result = self._results[path]
        if isinstance(result, Exception):
            raise result
        return result
