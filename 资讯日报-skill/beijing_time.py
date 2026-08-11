#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trusted UTC and Asia/Shanghai time independent of the host wall clock."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import json
import os
import random
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


UTC = dt.timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_ENDPOINTS = (
    "https://www.apple.com/",
    "https://www.cloudflare.com/",
    "https://www.google.com/generate_204",
)


class TimeAuthorityError(RuntimeError):
    """Raised when no trustworthy UTC value can be established."""


def boot_session_id() -> str:
    """Return the macOS boot-session UUID when available."""
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def monotonic_seconds() -> float:
    """System-wide monotonic time, including across short-lived Python processes."""
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return time.monotonic()


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _http_date(url: str, timeout: float = 4.0) -> dt.datetime:
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}ribao_time_nonce={random.randrange(10**12)}",
        method="HEAD",
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "ribao-beijing-guard/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.headers.get("Date", "")
    if not raw:
        raise TimeAuthorityError(f"time endpoint has no Date header: {url}")
    parsed = email.utils.parsedate_to_datetime(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def network_utc(
    endpoints=DEFAULT_ENDPOINTS,
    timeout: float = 4.0,
    tolerance_seconds: float = 5.0,
    fetcher=None,
) -> tuple[dt.datetime, list[dict]]:
    """Return quorum UTC and per-endpoint observations.

    At least two samples must fall within ``tolerance_seconds``. The median of
    the largest agreeing cluster is used, so a cached or otherwise bad Date
    header cannot move the workflow clock by itself.
    """

    fetch = fetcher or _http_date
    observations: list[dict] = []

    def one(url):
        try:
            value = fetch(url, timeout)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            value = value.astimezone(UTC)
            return {"url": url, "utc": value.isoformat(), "epoch": value.timestamp(), "ok": True}
        except Exception as exc:  # noqa: BLE001 - observations preserve the failure
            return {"url": url, "ok": False, "error": str(exc)[:300]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tuple(endpoints))) as pool:
        observations = list(pool.map(one, endpoints))

    epochs = sorted(item["epoch"] for item in observations if item.get("ok"))
    best: list[float] = []
    for index, start in enumerate(epochs):
        cluster = [value for value in epochs[index:] if value - start <= tolerance_seconds]
        if len(cluster) > len(best):
            best = cluster
    if len(best) < 2:
        raise TimeAuthorityError("fewer than two network time samples agree")

    chosen = dt.datetime.fromtimestamp(statistics.median(best), tz=UTC)
    return chosen, observations


class TrustedClock:
    """Network UTC with a same-boot monotonic fallback."""

    def __init__(
        self,
        anchor_path: Path,
        max_fallback_seconds: int = 7 * 24 * 3600,
        network_refresh_seconds: int = 5 * 60,
    ):
        self.anchor_path = Path(anchor_path)
        self.max_fallback_seconds = max_fallback_seconds
        self.network_refresh_seconds = network_refresh_seconds

    def _save_anchor(self, now_utc: dt.datetime, observations: list[dict]) -> None:
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "utc_epoch": now_utc.timestamp(),
            "monotonic": monotonic_seconds(),
            "boot_session": boot_session_id(),
            "observations": observations,
        }
        temporary = self.anchor_path.with_suffix(self.anchor_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.anchor_path)

    def _fallback(self) -> tuple[dt.datetime, dict]:
        try:
            payload = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            saved_boot = payload.get("boot_session", "")
            current_boot = boot_session_id()
            if saved_boot and current_boot and saved_boot != current_boot:
                raise TimeAuthorityError("trusted monotonic anchor belongs to an earlier boot")
            saved_mono = float(payload["monotonic"])
            current_mono = monotonic_seconds()
            elapsed = current_mono - saved_mono
            if elapsed < 0 or elapsed > self.max_fallback_seconds:
                raise TimeAuthorityError("trusted monotonic anchor expired or belongs to an earlier boot")
            now_utc = dt.datetime.fromtimestamp(float(payload["utc_epoch"]) + elapsed, tz=UTC)
            return now_utc, {"mode": "monotonic_fallback", "age_seconds": round(elapsed, 3)}
        except TimeAuthorityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TimeAuthorityError(f"trusted time anchor unavailable: {exc}") from exc

    def now(self, override_utc: str = "") -> tuple[dt.datetime, dict]:
        if override_utc:
            now_utc = parse_utc(override_utc)
            return now_utc, {"mode": "injected_test_time", "observations": []}
        try:
            cached_utc, cached_meta = self._fallback()
            if cached_meta["age_seconds"] <= self.network_refresh_seconds:
                cached_meta["mode"] = "monotonic_cached"
                return cached_utc, cached_meta
        except TimeAuthorityError:
            pass
        try:
            now_utc, observations = network_utc()
            self._save_anchor(now_utc, observations)
            return now_utc, {"mode": "network_quorum", "observations": observations}
        except Exception as network_error:  # noqa: BLE001
            now_utc, meta = self._fallback()
            meta["network_error"] = str(network_error)[:300]
            return now_utc, meta


def to_beijing(now_utc: dt.datetime) -> dt.datetime:
    return now_utc.astimezone(BEIJING)
