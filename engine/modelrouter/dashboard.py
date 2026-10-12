"""Read-only adapter for openDashboard public REST evidence.

The published 2.0 MCP normalizes price sets; the REST deployment can still return
legacy prices. This boundary understands both without interpreting a conditional
quote as a universally available token rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from .providers import UA, _per_m


@dataclass(frozen=True)
class PriceCard:
    prompt: Decimal | None = None
    completion: Decimal | None = None
    cached: Decimal | None = None
    read_at: str | None = None
    window: str | None = None


def _date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date if date.tzinfo else None
    except ValueError:
        return None


def prices(row: dict, window: str | None = None) -> PriceCard:
    if "pricePoints" not in row:
        quoted_window = row.get("pricingWindow")
        if quoted_window is not None:
            if not isinstance(quoted_window, str) or quoted_window.lower() not in (
                "asap", "balanced", "flex") or window is None or quoted_window.lower() != window.lower():
                return PriceCard()
        legacy = row.get("pricing") or {}
        if not isinstance(legacy, dict) or _date(row.get("lastConfirmedAt")) is None:
            return PriceCard()
        return PriceCard(_per_m(legacy.get("promptUsdPerToken")),
                         _per_m(legacy.get("completionUsdPerToken")),
                         _per_m(legacy.get("cachedPromptUsdPerToken")),
                         row.get("lastConfirmedAt"),
                         quoted_window.lower() if quoted_window else None)
    if row.get("pricingState") != "published" or not isinstance(row["pricePoints"], list):
        return PriceCard()
    grouped: dict[str, list] = {"token_in": [], "token_out": [], "token_cached": []}
    for point in row["pricePoints"]:
        if not isinstance(point, dict) or point.get("unit") not in grouped:
            continue
        condition = point.get("condition")
        if condition is not None:
            if not isinstance(condition, dict):
                continue
            kind = condition.get("kind")
            if kind == "latency_window":
                if window is None or str(condition.get("name", "")).lower() != window.lower():
                    continue
            elif kind == "rate_class" and condition.get("name") == "list":
                pass
            elif kind == "price_scope" and condition.get("name") == "public_list" \
                    and condition.get("rateClass", "list") == "list":
                pass
            else:
                continue
        source = point.get("source") or {}
        if not isinstance(source, dict):
            continue
        date = source.get("readAt")
        url = source.get("url")
        if not isinstance(url, str):
            continue
        try:
            parsed_url = urlsplit(url)
        except ValueError:
            continue
        if not parsed_url.netloc or parsed_url.scheme not in ("http", "https") \
                or _date(date) is None or point.get("provenance") not in (
                    "published", "derived", "parsed_from_prose"):
            continue
        grouped[point["unit"]].append((_per_m(point.get("amount")), date, condition))
    values, dates, selected_window = {}, [], None
    for unit, points in grouped.items():
        values[unit] = points[0][0] if len(points) == 1 else None
        if values[unit] is not None:
            dates.append(points[0][1])
            if points[0][2] and points[0][2].get("kind") == "latency_window":
                selected_window = window
    read_at = min(dates, key=_date) if dates else None
    return PriceCard(values["token_in"], values["token_out"], values["token_cached"],
                     read_at, selected_window)


def strings(value) -> tuple[str, ...] | None:
    return tuple(value) if isinstance(value, (list, tuple)) and all(
        isinstance(v, str) for v in value) else None


def capabilities(row: dict, native: dict | None = None) -> dict:
    native = native or {}
    params = strings(native.get("supportedParameters"))
    arch = native.get("architecture") or {}
    efforts = strings(row.get("reasoningEfforts"))
    context = row.get("contextLength") or native.get("contextLength")
    try:
        context = int(context) if not isinstance(context, bool) else None
        if context is not None and context <= 0:
            context = None
    except (ValueError, TypeError):
        context = None
    return {
        "context_length": context,
        "supports_tools": None if params is None else "tools" in params,
        "supports_reasoning": True if efforts is not None else (
            True if params is not None and set(params) & {
                "reasoning", "reasoning_effort", "include_reasoning"} else None),
        "supported_parameters": params,
        "reasoning_efforts": efforts,
        "input_modalities": strings(arch.get("input_modalities")),
        "output_modalities": strings(row.get("outputModalities"))
            if row.get("outputModalities") is not None else strings(arch.get("output_modalities")),
        "supports_editing": None,
    }


def fetch_rows(client: httpx.Client, base: str, collection: str,
               params: dict, max_pages: int = 100) -> list[dict]:
    """Fetch a complete collection; retain opaque cursors and never cache partial rows."""
    rows, seen = [], set()
    query = {**params, "limit": 500}
    for _ in range(max_pages):
        response = client.get(base.rstrip("/") + "/api/public/v2/" + collection,
                              params=query, headers={"User-Agent": UA})
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("invalid dashboard collection envelope")
        if body.get("schemaVersion", "2.0") != "2.0":
            raise ValueError("unsupported dashboard schema version")
        page = body["data"]
        if not all(isinstance(row, dict) for row in page):
            raise ValueError("invalid dashboard collection row")
        rows.extend(page)
        cursor = body.get("cursor")
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or not cursor or cursor in seen or not page:
            raise ValueError("invalid or repeated dashboard cursor")
        seen.add(cursor)
        query["cursor"] = cursor
    raise ValueError("dashboard cursor exceeded page limit")
