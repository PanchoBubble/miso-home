#!/usr/bin/env python3
"""Run repeatable Miso-oriented Ollama latency and throughput benchmarks."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from urllib.request import Request, urlopen

CASES = (
    (
        "bilingual_short",
        "Reply in Spanish with exactly one short sentence: Is the kitchen light on?",
        (),
    ),
    (
        "routine_tool",
        "Set a timer for five seconds. Use the provided tool and no prose.",
        (
            {
                "type": "function",
                "function": {
                    "name": "timer_create",
                    "description": "Create a countdown timer",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "duration_seconds": {"type": "integer", "minimum": 1},
                            "title": {"type": "string"},
                        },
                        "required": ["duration_seconds"],
                        "additionalProperties": False,
                    },
                },
            },
        ),
    ),
    (
        "reasoning_short",
        "In two concise sentences, compare SQLite WAL mode with rollback journals for a home assistant.",
        (),
    ),
)


def run(base_url: str, model: str, case: tuple, timeout: float) -> dict[str, object]:
    name, prompt, tools = case
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0, "num_predict": 96},
    }
    if tools:
        payload["tools"] = list(tools)
    request = Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    first_output = None
    final = {}
    text = []
    tool_name = None
    with urlopen(request, timeout=timeout) as response:
        for line in response:
            event = json.loads(line)
            message = event.get("message") or {}
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if (content or calls) and first_output is None:
                first_output = time.monotonic()
            if content:
                text.append(content)
            if calls:
                tool_name = calls[0].get("function", {}).get("name")
            if event.get("done"):
                final = event
    finished = time.monotonic()
    evaluation_seconds = final.get("eval_duration", 0) / 1_000_000_000
    evaluation_count = int(final.get("eval_count", 0))
    return {
        "model": model,
        "case": name,
        "time_to_first_output_seconds": (
            round(first_output - started, 3) if first_output is not None else None
        ),
        "total_seconds": round(finished - started, 3),
        "output_tokens": evaluation_count,
        "tokens_per_second": (
            round(evaluation_count / evaluation_seconds, 2)
            if evaluation_seconds > 0
            else None
        ),
        "tool_name": tool_name,
        "tool_correct": tool_name == "timer_create" if name == "routine_tool" else None,
        "text_characters": len("".join(text)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="+")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=180)
    arguments = parser.parse_args()
    results = []
    for model in arguments.models:
        run(arguments.base_url, model, ("warmup", "Reply only: ready", ()), arguments.timeout)
        for case in CASES:
            result = run(arguments.base_url, model, case, arguments.timeout)
            results.append(result)
            print(json.dumps(result, separators=(",", ":")), flush=True)
    for model in arguments.models:
        model_results = [item for item in results if item["model"] == model]
        first_output = [
            item["time_to_first_output_seconds"]
            for item in model_results
            if item["time_to_first_output_seconds"] is not None
        ]
        throughput = [
            item["tokens_per_second"]
            for item in model_results
            if item["tokens_per_second"] is not None
        ]
        print(
            json.dumps(
                {
                    "model": model,
                    "summary": True,
                    "median_time_to_first_output_seconds": round(
                        statistics.median(first_output), 3
                    ),
                    "median_tokens_per_second": round(statistics.median(throughput), 2),
                    "routine_tool_correct": next(
                        item["tool_correct"]
                        for item in model_results
                        if item["case"] == "routine_tool"
                    ),
                },
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
