#!/usr/bin/env python3
"""Extract one completed Codex task's usage and artifact facts from a rollout JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def timestamp_kst(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(KST).isoformat(timespec="milliseconds")


def extract(log_path: Path, artifact_path: Path, turn_id: str | None, expected_model: str | None = None) -> dict:
    starts: dict[str, dict] = {}
    contexts: dict[str, dict] = {}
    completions: list[dict] = []
    usage_records: dict[str, list[dict]] = {}

    with log_path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_no}: {error}") from error
            payload = row.get("payload") or {}
            row_type = row.get("type")
            kind = payload.get("type")
            current_id = payload.get("turn_id")

            if row_type == "turn_context" and current_id:
                contexts[current_id] = payload
            elif row_type == "token_usage_record" and current_id:
                usage_records.setdefault(current_id, []).append(
                    {"line": line_no, "usage": payload.get("turn_token_usage")}
                )
            elif row_type == "event_msg" and kind == "task_started" and current_id:
                starts[current_id] = {"timestamp": row.get("timestamp"), "line": line_no}
            elif row_type == "event_msg" and kind == "task_complete" and current_id:
                completions.append(
                    {"turn_id": current_id, "timestamp": row.get("timestamp"), "line": line_no, "payload": payload}
                )

    if not completions:
        raise ValueError("No completed task exists in this rollout log.")
    if turn_id is None:
        if len(completions) != 1:
            choices = ", ".join(item["turn_id"] for item in completions)
            raise ValueError(f"Multiple completed tasks: {choices}. Pass --turn-id for the Alice page task.")
        completion = completions[0]
    else:
        matches = [item for item in completions if item["turn_id"] == turn_id]
        if len(matches) != 1:
            choices = ", ".join(item["turn_id"] for item in completions)
            raise ValueError(f"Completed turn {turn_id!r} not found. Available: {choices}")
        completion = matches[0]

    selected_id = completion["turn_id"]
    candidates = [
        item for item in usage_records.get(selected_id, []) if item["line"] < completion["line"]
    ]
    if not candidates or not isinstance(candidates[-1]["usage"], dict):
        raise ValueError(f"No turn usage record before completion for {selected_id}.")
    usage = candidates[-1]["usage"]
    if any(not isinstance(usage.get(key), int) for key in TOKEN_KEYS):
        raise ValueError("The selected usage record lacks numeric token fields.")
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise ValueError("Token total does not equal input + output.")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise ValueError("Cached input exceeds input tokens.")
    if usage["reasoning_output_tokens"] > usage["output_tokens"]:
        raise ValueError("Reasoning output exceeds output tokens.")

    completion_payload = completion["payload"]
    duration_ms = completion_payload.get("duration_ms")
    if not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("The task completion record lacks a valid duration_ms.")
    first_token_ms = completion_payload.get("time_to_first_token_ms")
    if first_token_ms is not None and (not isinstance(first_token_ms, int) or first_token_ms < 0):
        raise ValueError("Invalid time_to_first_token_ms in completion record.")

    artifact_bytes = artifact_path.read_bytes()
    artifact_text = artifact_bytes.decode("utf-8")
    context = contexts.get(selected_id, {})
    recorded_model = context.get("model")
    if expected_model is not None:
        if artifact_path.parent.name != expected_model:
            raise ValueError(f"Artifact folder {artifact_path.parent.name!r} does not match model {expected_model!r}.")
        if recorded_model != expected_model:
            raise ValueError(f"Recorded model {recorded_model!r} does not match expected model {expected_model!r}.")
    return {
        "scope": "one completed Alice HTML task; excludes subsequent report creation",
        "turn_id": selected_id,
        "model": recorded_model,
        "reasoning_effort": context.get("effort"),
        "started_at_kst": timestamp_kst(starts.get(selected_id, {}).get("timestamp")),
        "completed_at_kst": timestamp_kst(completion["timestamp"]),
        "duration_ms": duration_ms,
        "time_to_first_token_ms": first_token_ms,
        "tokens": {
            "total": usage["total_tokens"],
            "input": usage["input_tokens"],
            "cached_input": usage["cached_input_tokens"],
            "uncached_input": usage["input_tokens"] - usage["cached_input_tokens"],
            "output": usage["output_tokens"],
            "reasoning_output_subset": usage["reasoning_output_tokens"],
            "cached_share_of_input_percent": round(
                100 * usage["cached_input_tokens"] / usage["input_tokens"], 2
            ) if usage["input_tokens"] else None,
        },
        "artifact": {
            "name": artifact_path.name,
            "bytes": len(artifact_bytes),
            "lines": len(artifact_text.splitlines()),
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        },
        "sources": {
            "rollout_log": log_path.name,
            "usage_record_line": candidates[-1]["line"],
            "task_complete_line": completion["line"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path, help="Codex rollout JSONL file")
    parser.add_argument("--artifact", required=True, type=Path, help="Finished Alice HTML file")
    parser.add_argument("--expected-model", help="Require this recorded model and artifact folder name")
    parser.add_argument("--turn-id", help="Completed page task ID; required if log has multiple completions")
    parser.add_argument("--output", type=Path, help="Write the JSON here as well as to stdout")
    args = parser.parse_args()
    try:
        data = extract(args.log, args.artifact, args.turn_id, args.expected_model)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    rendered = json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
