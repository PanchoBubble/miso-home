"""Codex CLI adapter that shells out to `codex exec` for the model lane.

The CLI owns its own credentials: Miso runs the binary and reads its JSON
event stream, and never reads, copies, or reuses the OAuth token the CLI
stores. Every run is pinned to a read-only sandbox in a throwaway empty
directory so a spoken question can never reach the host filesystem.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from threading import Event, Thread

from miso.providers.base import (
    ChatChunk,
    ChatRequest,
    GenerationMetrics,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderProtocolError,
)

_ANSWER_ONLY_PREAMBLE = (
    "You are the answering half of a household voice assistant. "
    "Reply with the spoken answer only: no preamble, no markdown, no code "
    "fences, no bullet lists, and no questions back. Never edit files, run "
    "commands, or describe what you are about to do. Keep it under three "
    "short sentences."
)

_DELTA_EVENTS = frozenset(
    {"agent_message_delta", "item.delta", "response.output_text.delta"}
)
_POLL_SECONDS = 0.05
_STDERR_TAIL_LINES = 20
_TERMINATE_GRACE_SECONDS = 2.0


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _decode_event(payload: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    """Normalise both Codex event schemas to a (type, body) pair.

    Older builds wrap every event as {"id": ..., "msg": {"type": ...}}, newer
    ones emit the type at the top level. Reading both keeps the provider from
    breaking on a CLI upgrade the Pi picks up on its own.
    """
    body = payload.get("msg")
    if isinstance(body, Mapping):
        kind = body.get("type")
        return (kind if isinstance(kind, str) else ""), body
    kind = payload.get("type")
    return (kind if isinstance(kind, str) else ""), payload


def _token_source(body: Mapping[str, object]) -> Mapping[str, object] | None:
    """Find the token counters wherever this CLI build nests them."""
    info = body.get("info")
    nested = info.get("total_token_usage") if isinstance(info, Mapping) else None
    for candidate in (body.get("usage"), nested, info, body):
        if isinstance(candidate, Mapping) and any(
            key in candidate for key in ("input_tokens", "output_tokens")
        ):
            return candidate
    return None


def _usage_metrics(
    body: Mapping[str, object],
    prompt_milliseconds: int,
    generation_milliseconds: int,
) -> GenerationMetrics | None:
    source = _token_source(body)
    if source is None:
        return None
    return GenerationMetrics(
        prompt_tokens=_count(source.get("input_tokens")),
        prompt_milliseconds=prompt_milliseconds,
        generated_tokens=_count(source.get("output_tokens")),
        generation_milliseconds=generation_milliseconds,
    )


def build_prompt(messages: Sequence[Mapping[str, str]]) -> str:
    """Flatten a chat history into the single prompt `codex exec` accepts."""
    lines = [_ANSWER_ONLY_PREAMBLE, ""]
    system = [
        str(message.get("content", "")).strip()
        for message in messages
        if message.get("role") == "system" and str(message.get("content", "")).strip()
    ]
    if system:
        lines.append("Context:")
        lines.extend(system)
        lines.append("")
    turns = [message for message in messages if message.get("role") != "system"]
    if turns:
        lines.append("Conversation:")
        for message in turns:
            role = "User" if message.get("role") == "user" else "Assistant"
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        lines.append("")
    lines.append("Answer:")
    return "\n".join(lines)


class _StderrTail:
    """Drain the CLI's stderr in the background so a full pipe cannot wedge it."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._thread = Thread(
            target=self._drain,
            args=(process.stderr,),
            name="miso-codex-stderr",
            daemon=True,
        )
        self._thread.start()

    def _drain(self, stream: object) -> None:
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.decode("utf-8", "replace").strip()
                if line:
                    self._lines.append(line)
        except (OSError, ValueError):
            return

    def text(self) -> str:
        self._thread.join(timeout=_TERMINATE_GRACE_SECONDS)
        return " | ".join(self._lines)[:240] or "no stderr output"


class CodexCliProvider:
    """Run the signed-in Codex CLI as a provider, or skip when it is not usable."""

    def __init__(
        self,
        binary: str = "codex",
        model: str | None = None,
        timeout: float = 120,
    ) -> None:
        self.binary = binary
        self.model = model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "codex-cli"

    def health(self) -> ProviderHealth:
        executable = self._executable()
        if executable is None:
            return ProviderHealth(False, "binary_not_found", self.model)
        try:
            completed = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                timeout=max(1.0, min(self.timeout, 5.0)),
                env=self._environment(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProviderHealth(False, "unavailable:login_status_timeout", self.model)
        except OSError as error:
            return ProviderHealth(
                False, f"unavailable:{type(error).__name__}", self.model
            )
        if completed.returncode != 0:
            return ProviderHealth(False, "not_authenticated", self.model)
        return ProviderHealth(True, "ready", self.model)

    def stream(self, request: ChatRequest, cancel: Event) -> Iterator[ChatChunk]:
        if cancel.is_set():
            raise ProviderCancelled("request cancelled before dispatch")
        executable = self._executable()
        if executable is None:
            raise ProviderError("codex CLI binary is not installed")
        with tempfile.TemporaryDirectory(prefix="miso-codex-") as workdir:
            yield from self._run(
                executable,
                workdir,
                build_prompt(request.messages),
                request.model or self.model,
                cancel,
            )

    def _run(
        self,
        executable: str,
        workdir: str,
        prompt: str,
        model: str | None,
        cancel: Event,
    ) -> Iterator[ChatChunk]:
        argv = [
            executable,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--cd",
            workdir,
        ]
        if model:
            argv.extend(["--model", model])
        argv.append("-")
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=self._environment(),
            )
        except OSError as error:
            raise ProviderError(
                f"codex exec failed to start: {type(error).__name__}"
            ) from error
        stderr = _StderrTail(process)
        started = time.monotonic()
        try:
            self._send_prompt(process, prompt)
            yield from self._consume(process, cancel, started, stderr)
        finally:
            self._stop(process)

    def _send_prompt(self, process: subprocess.Popen, prompt: str) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except OSError as error:
            raise ProviderError(
                f"codex exec rejected the prompt: {type(error).__name__}"
            ) from error

    def _consume(
        self,
        process: subprocess.Popen,
        cancel: Event,
        started: float,
        stderr: "_StderrTail",
    ) -> Iterator[ChatChunk]:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = started + self.timeout
        buffered = b""
        streamed = False
        final_text = ""
        completed = False
        metrics: GenerationMetrics | None = None
        first_text_at: float | None = None
        usage: Mapping[str, object] = {}
        try:
            while True:
                if cancel.is_set():
                    raise ProviderCancelled("request cancelled")
                if time.monotonic() > deadline:
                    raise ProviderError("codex exec exceeded its timeout")
                if not selector.select(timeout=_POLL_SECONDS):
                    continue
                data = process.stdout.read1(65_536)
                if not data:
                    break
                buffered += data
                while b"\n" in buffered:
                    raw, buffered = buffered.split(b"\n", 1)
                    event = self._decode_line(raw)
                    if event is None:
                        continue
                    kind, body = event
                    text = self._delta(kind, body)
                    if text:
                        if first_text_at is None:
                            first_text_at = time.monotonic()
                        streamed = True
                        yield ChatChunk(text=text)
                        continue
                    message = self._final_message(kind, body)
                    if message:
                        final_text = message
                    if kind in {"error", "stream_error", "turn.failed"}:
                        raise ProviderError(
                            f"codex exec reported {kind}: {self._reason(body)}"
                        )
                    if _token_source(body) is not None and not self._delta(kind, body):
                        usage = body
                    if kind in {"task_complete", "turn.completed"}:
                        completed = True
                        prompt_ms, generation_ms = self._split_latency(
                            started, first_text_at
                        )
                        metrics = _usage_metrics(
                            usage or body, prompt_ms, generation_ms
                        )
        finally:
            selector.close()
        returncode = self._wait(process)
        if returncode != 0:
            raise ProviderError(
                f"codex exec exited with {returncode}: {stderr.text()}"
            )
        if not completed:
            raise ProviderProtocolError("codex exec ended without a completion event")
        if not streamed:
            if not final_text:
                raise ProviderProtocolError("codex exec produced no answer")
            yield ChatChunk(text=final_text)
        yield ChatChunk(done=True, metrics=metrics)

    @staticmethod
    def _decode_line(raw: bytes) -> tuple[str, Mapping[str, object]] | None:
        line = raw.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except ValueError:
            # The CLI prefixes its stream with human-readable banner lines.
            return None
        if not isinstance(payload, Mapping):
            return None
        return _decode_event(payload)

    @staticmethod
    def _delta(kind: str, body: Mapping[str, object]) -> str:
        if kind not in _DELTA_EVENTS:
            return ""
        for key in ("delta", "text"):
            value = body.get(key)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _final_message(kind: str, body: Mapping[str, object]) -> str:
        if kind == "agent_message":
            value = body.get("message")
            return value if isinstance(value, str) else ""
        if kind == "item.completed":
            item = body.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                value = item.get("text")
                return value if isinstance(value, str) else ""
            return ""
        if kind in {"task_complete", "turn.completed"}:
            value = body.get("last_agent_message")
            return value if isinstance(value, str) else ""
        return ""

    @staticmethod
    def _reason(body: Mapping[str, object]) -> str:
        for key in ("message", "error", "reason"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:200]
        return "no detail"

    @staticmethod
    def _split_latency(started: float, first_text_at: float | None) -> tuple[int, int]:
        """Approximate prompt versus generation time from the stream itself.

        The CLI reports token counts but no phase timings, so time to the first
        visible token stands in for prompt evaluation and the rest for
        generation. It is the same split the other providers report, measured
        from the outside.
        """
        finished = time.monotonic()
        if first_text_at is None:
            return max(0, round((finished - started) * 1000)), 0
        return (
            max(0, round((first_text_at - started) * 1000)),
            max(0, round((finished - first_text_at) * 1000)),
        )

    def _wait(self, process: subprocess.Popen) -> int:
        try:
            return process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=_TERMINATE_GRACE_SECONDS)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    continue

    def _executable(self) -> str | None:
        candidate = self.binary.strip()
        if not candidate:
            return None
        if os.path.sep in candidate:
            return candidate if os.access(candidate, os.X_OK) else None
        return shutil.which(candidate)

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment["NO_COLOR"] = "1"
        return environment
