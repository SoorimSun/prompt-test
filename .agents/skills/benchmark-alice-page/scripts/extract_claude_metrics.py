#!/usr/bin/env python3
"""Extract one completed Claude Code turn's usage, time, API-equivalent cost and artifact facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

KST = timezone(timedelta(hours=9))
DEFAULT_PRICING = Path(__file__).resolve().parent.parent / "references" / "claude-pricing.json"
MILLION = Decimal(1_000_000)
# (key in the output, rate key in the price table); every prompt and output token is in exactly one class.
TOKEN_CLASSES = (
    ("uncached_input", "input"),
    ("cache_write_5m", "cache_write_5m"),
    ("cache_write_1h", "cache_write_1h"),
    ("cached_input", "cache_read"),
    ("output", "output"),
)


class Unpriced(Exception):
    """This request cannot be priced from the price table without guessing."""


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def to_kst(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="milliseconds")


def base_id(model: str) -> str:
    """Model ID without a trailing snapshot date, e.g. claude-haiku-4-5-20251001 -> claude-haiku-4-5."""
    return re.sub(r"-\d{8}$", "", model)


def folder_key(name: str) -> str:
    """Make `fable-5.2`, `claude-fable-5-2` and `claude-fable-5-2-<date>` comparable."""
    return base_id(name.lower()).removeprefix("claude-").replace(".", "-")


def money(amount: Decimal) -> str:
    return format(amount.normalize(), "f")


def read_rows(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_no} of {path.name}: {error}") from error


def add_call(calls: dict, row: dict, line_no: int, log: str, main: bool) -> None:
    """Record one API response. A response is logged once per content block; count it once."""
    message = row.get("message") or {}
    usage = message.get("usage")
    if message.get("model") in (None, "<synthetic>") or not isinstance(usage, dict):
        return
    key = message.get("id") or row.get("requestId") or f"{log}:{line_no}"
    call = calls.setdefault(key, {"id": key, "log": log, "line": line_no, "main": main})
    call.update(
        last_line=line_no,
        timestamp=row.get("timestamp"),
        model=message["model"],
        usage=usage,
        stop_reason=message.get("stop_reason"),
        effort=row.get("effort"),
    )


def collect_turns(log_path: Path) -> dict[str, dict]:
    """Group the session by promptId: every record belongs to the most recent prompt."""
    turns: dict[str, dict] = {}
    current = None
    for line_no, row in read_rows(log_path):
        kind = row.get("type")
        if kind == "user" and not row.get("isSidechain") and row.get("promptId"):
            current = turns.setdefault(
                row["promptId"],
                {"turn_id": row["promptId"], "prompt_line": line_no, "started": row.get("timestamp"),
                 "calls": {}, "durations": []},
            )
        elif current is None:
            continue
        elif kind == "assistant":
            add_call(current["calls"], row, line_no, log_path.name, main=not row.get("isSidechain"))
        elif kind == "system" and row.get("subtype") == "turn_duration":
            current["durations"].append(
                {"ms": row.get("durationMs"), "line": line_no, "timestamp": row.get("timestamp")}
            )
    return turns


def select_turn(turns: dict[str, dict], turn_id: str | None) -> dict:
    candidates = [turn for turn in turns.values() if any(call["main"] for call in turn["calls"].values())]
    if not candidates:
        raise ValueError("No turn with API responses exists in this session log.")
    choices = "; ".join(
        f"{turn['turn_id']} (started {to_kst(parse_time(turn['started'])) if turn['started'] else 'unknown'}, "
        f"{len(turn['calls'])} API responses)"
        for turn in candidates
    )
    if turn_id is None:
        if len(candidates) != 1:
            raise ValueError(f"Multiple turns: {choices}. Pass --turn-id for the Alice page task.")
        return candidates[0]
    matches = [turn for turn in candidates if turn["turn_id"] == turn_id]
    if len(matches) != 1:
        raise ValueError(f"Turn {turn_id!r} not found. Available: {choices}")
    return matches[0]


def add_subagent_calls(turn: dict, log_path: Path, start: datetime, end: datetime) -> list[str]:
    """Subagents write their own logs beside the session; add their responses made during the turn."""
    folder = log_path.with_suffix("") / "subagents"
    used = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.jsonl")):
            before = len(turn["calls"])
            for line_no, row in read_rows(path):
                moment = parse_time(row.get("timestamp"))
                if row.get("type") == "assistant" and moment is not None and start <= moment <= end:
                    add_call(turn["calls"], row, line_no, path.name, main=False)
            if len(turn["calls"]) > before:
                used.append(path.name)
    return used


def count_tokens(call: dict) -> dict:
    """Token counts for one response. With several usage.iterations, the iterations are the billed truth."""
    usage = call["usage"]
    iterations = usage.get("iterations") or []
    single = len(iterations) <= 1 and all(item.get("type") == "message" for item in iterations)
    parts = [usage] if single else iterations

    def total(key: str, required: bool = False) -> int:
        values = [part.get(key) for part in parts]
        if not required:
            values = [0 if value is None else value for value in values]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise ValueError(f"Usage on line {call['line']} of {call['log']} lacks a numeric {key}.")
        return sum(values)

    counts = {
        "uncached_input": total("input_tokens", required=True),
        "cache_write_input": total("cache_creation_input_tokens"),
        "cached_input": total("cache_read_input_tokens"),
        "output": total("output_tokens", required=True),
    }
    breakdowns = [part.get("cache_creation") for part in parts]
    counts["cache_write_5m"] = sum((b or {}).get("ephemeral_5m_input_tokens") or 0 for b in breakdowns)
    counts["cache_write_1h"] = sum((b or {}).get("ephemeral_1h_input_tokens") or 0 for b in breakdowns)
    counts["ttl_known"] = counts["cache_write_5m"] + counts["cache_write_1h"] == counts["cache_write_input"]
    counts["thinking"] = (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
    counts["prompt"] = counts["uncached_input"] + counts["cache_write_input"] + counts["cached_input"]
    counts["single"] = single
    counts["iteration_types"] = [item.get("type") for item in iterations]
    return counts


def find_rates(call: dict, counts: dict, pricing: dict) -> tuple[str, int, dict, Decimal]:
    """Return (speed, tier index, tier rates, geo multiplier) for one request, or raise Unpriced."""
    usage = call["usage"]
    if not counts["single"]:
        raise Unpriced(
            f"usage.iterations has {len(counts['iteration_types'])} entries "
            f"({', '.join(map(str, counts['iteration_types']))}); per-attempt billing is not derived"
        )
    if usage.get("fallback_credit") is not None:
        raise Unpriced("fallback_credit is set; the request was repriced")
    service_tier = usage.get("service_tier")
    if service_tier not in (None, "standard"):
        raise Unpriced(f"service_tier {service_tier!r} has no listed price")
    model = call["model"]
    speeds = pricing["models"].get(model) or pricing["models"].get(base_id(model))
    if speeds is None:
        raise Unpriced(f"no listed price for model {model!r}")
    speed = usage.get("speed") or "standard"
    tiers = speeds.get(speed)
    if tiers is None:
        raise Unpriced(f"no {speed} price listed for {model}")
    geo = usage.get("inference_geo")
    multiplier = "1" if geo is None else (pricing.get("inference_geo_multipliers") or {}).get(geo)
    if multiplier is None:
        raise Unpriced(f"inference_geo {geo!r} has no listed multiplier")
    if not counts["ttl_known"]:
        raise Unpriced("cache write has no TTL breakdown (5m/1h)")
    for index, tier in enumerate(tiers):
        limit = tier.get("max_prompt_tokens")
        if limit is None or counts["prompt"] <= limit:
            return speed, index, tier, Decimal(multiplier)
    raise Unpriced(f"prompt of {counts['prompt']} tokens is above every listed tier for {model}")


def estimate_cost(calls: list[dict], pricing: dict) -> dict:
    """Price every request on its own: list price x recorded tokens. An estimate, never a bill."""
    by_class = {name: Decimal(0) for name, _ in TOKEN_CLASSES}
    groups: dict[tuple, dict] = {}
    unpriced: dict[tuple, dict] = {}
    other_geos: dict[str | None, list] = {}
    server_tools = {"web_search_requests": 0, "web_fetch_requests": 0}
    for call in calls:
        counts = call["counts"]
        for name in server_tools:
            server_tools[name] += (call["usage"].get("server_tool_use") or {}).get(name) or 0
        try:
            speed, index, tier, multiplier = find_rates(call, counts, pricing)
        except Unpriced as reason:
            skipped = unpriced.setdefault(
                (str(reason), call["model"], call["log"]),
                {"reason": str(reason), "model": call["model"], "log": call["log"], "requests": 0, "lines": []},
            )
            skipped["requests"] += 1
            skipped["lines"].append(call["line"])
            continue
        geo = call["usage"].get("inference_geo")
        if geo != "global":
            other_geos.setdefault(geo, [0, multiplier])[0] += 1
        group = groups.setdefault(
            (call["model"], speed, index),
            {"model": call["model"], "speed": speed, "max_prompt_tokens": tier.get("max_prompt_tokens"),
             "requests": 0, "prompt_tokens": 0, "usd": Decimal(0)},
        )
        group["requests"] += 1
        group["prompt_tokens"] += counts["prompt"]
        for name, rate_key in TOKEN_CLASSES:
            amount = counts[name] * Decimal(tier[rate_key]) * multiplier / MILLION
            by_class[name] += amount
            group["usd"] += amount
    subtotal = sum(by_class.values(), Decimal(0))
    complete = not unpriced
    return {
        "basis": "API-equivalent estimate: list price x recorded tokens, priced per request; not a billed amount",
        "currency": pricing.get("currency", "USD"),
        "complete": complete,
        "total_usd": money(subtotal) if complete else None,
        "priced_subtotal_usd": money(subtotal),
        "breakdown_usd": {name: money(amount) for name, amount in by_class.items()},
        "by_price_tier": [{**groups[key], "usd": money(groups[key]["usd"])} for key in sorted(groups)],
        "max_single_request_prompt_tokens": max(call["counts"]["prompt"] for call in calls),
        "assumptions": [
            f"{count} request(s) recorded inference_geo {geo!r}; priced with multiplier {money(multiplier)}"
            for geo, (count, multiplier) in other_geos.items()
        ],
        "unpriced_requests": list(unpriced.values()),
        "not_included": {**server_tools, "note": "server tool fees, tax, discounts, subscription billing"},
        "pricing_source": pricing.get("source"),
        "pricing_checked_at_kst": pricing.get("checked_at_kst"),
    }


def extract(log_path: Path, artifact_path: Path, turn_id: str | None = None,
            expected_model: str | None = None, pricing: dict | None = None) -> dict:
    log_path, artifact_path = Path(log_path), Path(artifact_path)
    if pricing is None:
        pricing = json.loads(DEFAULT_PRICING.read_text(encoding="utf-8"))
    if not isinstance(pricing.get("models"), dict):
        raise ValueError("The price table has no 'models' object.")

    turn = select_turn(collect_turns(log_path), turn_id)
    selected_id = turn["turn_id"]
    started = parse_time(turn["started"])
    if started is None:
        raise ValueError(f"Turn {selected_id} has no start timestamp.")
    main_calls = [call for call in turn["calls"].values() if call["main"]]
    last_response = max(main_calls, key=lambda call: call["last_line"])

    # A finished turn has a turn_duration record; older logs only show a final end_turn response.
    durations = turn["durations"]
    if durations:
        completed = parse_time(durations[-1]["timestamp"])
    elif last_response["stop_reason"] == "end_turn":
        completed = parse_time(last_response["timestamp"])
    else:
        raise ValueError(
            f"Turn {selected_id} is not complete: no turn_duration record and the last response "
            f"ended with {last_response['stop_reason']!r}."
        )
    if completed is None:
        raise ValueError(f"Turn {selected_id} has no completion timestamp.")
    recorded_ms = durations[0]["ms"] if len(durations) == 1 else None
    if isinstance(recorded_ms, int) and not isinstance(recorded_ms, bool) and recorded_ms >= 0:
        duration_ms, duration_source = recorded_ms, "turn_duration"
    else:
        duration_ms, duration_source = round((completed - started).total_seconds() * 1000), "timestamps"

    subagent_logs = add_subagent_calls(turn, log_path, started, completed)
    calls = sorted(turn["calls"].values(), key=lambda call: (not call["main"], call["log"], call["line"]))
    for call in calls:
        call["counts"] = count_tokens(call)

    main_models = sorted({call["model"] for call in main_calls})
    if expected_model is not None:
        if folder_key(artifact_path.parent.name) != folder_key(expected_model):
            raise ValueError(f"Artifact folder {artifact_path.parent.name!r} does not match model {expected_model!r}.")
        if {base_id(model) for model in main_models} != {base_id(expected_model)}:
            raise ValueError(f"Recorded model(s) {main_models} do not match expected model {expected_model!r}.")
    by_model: dict[str, int] = {}
    for call in calls:
        by_model[call["model"]] = by_model.get(call["model"], 0) + 1
    efforts = sorted({call["effort"] for call in main_calls if call["effort"]})

    def tokens(name: str) -> int:
        return sum(call["counts"][name] for call in calls)

    ttl_known = all(call["counts"]["ttl_known"] for call in calls)
    input_tokens = tokens("prompt")
    artifact_bytes = artifact_path.read_bytes()
    artifact_text = artifact_bytes.decode("utf-8")
    return {
        "scope": "one completed Alice HTML task in a Claude Code session; excludes subsequent report creation",
        "harness": "claude-code",
        "turn_id": selected_id,
        "model": main_models[0] if len(main_models) == 1 else None,
        "reasoning_effort": ", ".join(efforts) or None,
        "started_at_kst": to_kst(started),
        "completed_at_kst": to_kst(completed),
        "duration_ms": duration_ms,
        "duration_source": duration_source,
        "time_to_first_token_ms": None,
        "api_calls": {
            "total": len(calls),
            "main": len(main_calls),
            "subagent": len(calls) - len(main_calls),
            "by_model": by_model,
        },
        "tokens": {
            "total": input_tokens + tokens("output"),
            "input": input_tokens,
            "uncached_input": tokens("uncached_input"),
            "cache_write_input": tokens("cache_write_input"),
            "cache_write_5m": tokens("cache_write_5m") if ttl_known else None,
            "cache_write_1h": tokens("cache_write_1h") if ttl_known else None,
            "cached_input": tokens("cached_input"),
            "output": tokens("output"),
            "reasoning_output_subset": tokens("thinking"),
            "cached_share_of_input_percent": round(100 * tokens("cached_input") / input_tokens, 2)
            if input_tokens else None,
        },
        "cost": estimate_cost(calls, pricing),
        "artifact": {
            "name": artifact_path.name,
            "bytes": len(artifact_bytes),
            "lines": len(artifact_text.splitlines()),
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        },
        "sources": {
            "session_log": log_path.name,
            "prompt_line": turn["prompt_line"],
            "turn_duration_line": durations[-1]["line"] if durations else None,
            "last_response_line": last_response["last_line"],
            "subagent_logs": subagent_logs,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path, help="Claude Code session JSONL file")
    parser.add_argument("--artifact", required=True, type=Path, help="Finished Alice HTML file")
    parser.add_argument("--expected-model", help="Require this recorded model ID and a matching artifact folder")
    parser.add_argument("--turn-id", help="promptId of the page task; required if the log has several turns")
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING, help="Price table JSON")
    parser.add_argument("--output", type=Path, help="Write the JSON here as well as to stdout")
    args = parser.parse_args()
    try:
        pricing = json.loads(args.pricing.read_text(encoding="utf-8"))
        data = extract(args.log, args.artifact, args.turn_id, args.expected_model, pricing)
    except (OSError, UnicodeError, ValueError, ArithmeticError, KeyError) as error:
        parser.exit(2, f"error: {error}\n")
    rendered = json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
