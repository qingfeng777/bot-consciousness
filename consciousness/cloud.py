"""Cloud-only chat-completions transport. No local weights or hidden fallback."""
import asyncio
from dataclasses import dataclass, field
import http.client
import json
import os
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit

from .domain import DomainError, InvalidInput, canonical
from .state import number


class CloudError(DomainError):
    code = "CLOUD_ERROR"


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError("nonfinite JSON number")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise CloudError("endpoint returned invalid JSON") from None


@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = 30
    max_tokens: int = 2048
    max_calls: int = 20
    max_concurrency: int = 2
    json_mode: bool = True
    stream: bool = False
    modalities: tuple = ("text",)
    models: dict = field(default_factory=dict)
    audio_data_url: bool = True
    extra_body: dict = field(default_factory=dict)
    _allow_http: bool = field(default=False, repr=False)

    def __post_init__(self):
        url = urlsplit(self.base_url)
        local_test = self._allow_http and url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (url.scheme != "https" and not local_test) or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise InvalidInput("model base URL must be HTTPS without credentials, query or fragment")
        if any(c in self.base_url for c in "\r\n"):
            raise InvalidInput("invalid model base URL")
        if not isinstance(self.api_key, str) or not self.api_key or any(c in self.api_key for c in "\r\n"):
            raise InvalidInput("cloud API key is missing or invalid")
        for value in [self.model, *self.models.values()]:
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise InvalidInput("cloud model ID is missing or invalid")
        number(self.timeout, 0.01, 120)
        for value, maximum in [(self.max_tokens, 8192), (self.max_calls, 1000), (self.max_concurrency, 8)]:
            if type(value) is not int or not 1 <= value <= maximum:
                raise InvalidInput("cloud limit outside range")
        if type(self.json_mode) is not bool or type(self.stream) is not bool:
            raise InvalidInput("cloud mode flags must be boolean")
        if type(self.audio_data_url) is not bool:
            raise InvalidInput("audio encoding flag must be boolean")
        if (not isinstance(self.extra_body, dict) or set(self.extra_body) - {"reasoning_effort", "thinking", "modalities", "temperature"}
                or len(canonical(self.extra_body)) > 2048):
            raise InvalidInput("unsupported provider-specific parameters")
        if "modalities" in self.extra_body and self.extra_body["modalities"] != ["text"]:
            raise InvalidInput("cloud gateway only accepts text output modality")
        if not set(self.modalities) <= {"text", "image", "audio"} or "text" not in self.modalities:
            raise InvalidInput("unsupported configured cloud modalities")

    @classmethod
    def from_env(cls):
        from .prompting import SCHEMAS
        # Explicit references reuse an operator's provider key without copying it.
        # Never guess a provider from unrelated credentials present in the shell.
        key_env = os.environ.get("BOT_MODEL_API_KEY_ENV", "")
        if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_env):
            raise InvalidInput("BOT_MODEL_API_KEY_ENV must name an environment variable")
        if key_env and os.environ.get("BOT_MODEL_API_KEY"):
            raise InvalidInput("configure only one of BOT_MODEL_API_KEY and BOT_MODEL_API_KEY_ENV")
        api_key = os.environ.get(key_env or "BOT_MODEL_API_KEY", "")
        missing = [name for name in ("BOT_MODEL_BASE_URL", "BOT_MODEL_NAME") if not os.environ.get(name)]
        if not api_key:
            missing.append("BOT_MODEL_API_KEY_ENV target" if key_env else "BOT_MODEL_API_KEY")
        if missing:
            raise InvalidInput("missing cloud configuration: " + ", ".join(missing))
        def flag(name, default):
            value = os.environ.get(name, default)
            if value not in {"true", "false"}:
                raise InvalidInput(name + " must be true or false")
            return value == "true"
        return cls(base_url=os.environ.get("BOT_MODEL_BASE_URL", ""), model=os.environ.get("BOT_MODEL_NAME", ""),
                   api_key=api_key,
                   timeout=float(os.environ.get("BOT_MODEL_TIMEOUT", "30")),
                   max_tokens=int(os.environ.get("BOT_MODEL_MAX_TOKENS", "2048")),
                   max_calls=int(os.environ.get("BOT_MODEL_MAX_CALLS", "20")),
                   max_concurrency=int(os.environ.get("BOT_MODEL_MAX_CONCURRENCY", "2")),
                   json_mode=flag("BOT_MODEL_JSON_MODE", "true"), stream=flag("BOT_MODEL_STREAM", "false"),
                   audio_data_url=flag("BOT_MODEL_AUDIO_DATA_URL", "true"),
                   extra_body=strict_json(os.environ.get("BOT_MODEL_EXTRA_BODY", "{}")),
                   modalities=tuple(os.environ.get("BOT_MODEL_MODALITIES", "text").split(",")),
                   models={p: os.environ["BOT_MODEL_" + p.upper()] for p in SCHEMAS if "BOT_MODEL_" + p.upper() in os.environ})


class _Request:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled = False
        self.connection = None
        self.socket = None

    def cancel(self):
        with self.lock:
            self.cancelled = True
            connection = self.connection
            if connection:
                try:
                    transport = self.socket or connection.sock
                    if transport:
                        transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()


class CloudClient:
    MAX_RESPONSE = 2 * 1024 * 1024

    def __init__(self, config):
        self.config = config
        self._slots = asyncio.Semaphore(config.max_concurrency)

    def model_for(self, purpose):
        return self.config.models.get(purpose, self.config.model)

    @staticmethod
    def _decode(raw, streaming):
        if not streaming:
            return strict_json(raw)
        content, finish, usage = [], None, {}
        for line in raw.decode("utf-8").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = strict_json(data)
            if not isinstance(chunk, dict):
                raise CloudError("invalid cloud stream chunk")
            usage = chunk.get("usage") or usage
            choices = chunk.get("choices", [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise CloudError("invalid cloud stream choices")
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            if delta.get("tool_calls") or delta.get("refusal"):
                raise CloudError("cloud refused or attempted a tool call")
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str):
                    raise CloudError("invalid cloud stream content")
                content.append(text)
            finish = choice.get("finish_reason") or finish
        return {"choices": [{"finish_reason": finish, "message": {"content": "".join(content)}}], "usage": usage}

    def _send(self, body, control):
        url = urlsplit(self.config.base_url)
        connection = (http.client.HTTPSConnection(url.hostname, url.port, timeout=self.config.timeout,
                                                  context=ssl.create_default_context()) if url.scheme == "https" else
                      http.client.HTTPConnection(url.hostname, url.port, timeout=self.config.timeout))
        with control.lock:
            if control.cancelled:
                raise CloudError("cloud request cancelled before send")
            control.connection = connection
        try:
            connection.connect()
            with control.lock:
                control.socket = connection.sock
                if control.cancelled:
                    raise CloudError("cloud request cancelled before send")
            connection.request("POST", url.path.rstrip("/") + "/chat/completions", body=body,
                               headers={"Authorization": "Bearer " + self.config.api_key,
                                        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            response = connection.getresponse()
            # No redirects or automatic retries, especially across authentication boundaries.
            if response.status != 200:
                raise CloudError("cloud endpoint returned HTTP " + str(response.status))
            raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise CloudError("cloud response exceeded transport budget")
            value = self._decode(raw, self.config.stream)
            if not isinstance(value, dict) or not isinstance(value.get("choices"), list) or len(value["choices"]) != 1:
                raise CloudError("cloud response has no unique completion")
            choice = value["choices"][0]
            message = choice.get("message", {})
            if choice.get("finish_reason") != "stop" or message.get("refusal") or message.get("tool_calls"):
                raise CloudError("cloud completion refused, truncated or not final")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
                raise CloudError("cloud completion empty or outside budget")
            usage = value.get("usage") or {}
            safe_usage = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                          if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            return {"content": content, "usage": safe_usage}
        except CloudError:
            raise
        except Exception:
            # Error bodies, raw URLs and exception strings may include credentials.
            raise CloudError("cloud transport or response failed") from None
        finally:
            connection.close()

    async def complete(self, purpose, messages):
        payload = dict(model=self.model_for(purpose), messages=messages, stream=self.config.stream,
                       max_tokens=self.config.max_tokens)
        payload.update(self.config.extra_body)
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.config.stream:
            payload["stream_options"] = {"include_usage": True}
        body = canonical(payload).encode()
        if len(body) > 12 * 1024 * 1024:
            raise InvalidInput("cloud request exceeded 12 MiB")
        return await self._request(body)

    async def _request(self, body):
        """Bound concurrency and transport lifetime for cloud requests."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(self._slots.acquire(), self.config.timeout)
        remaining = self.config.timeout - (loop.time() - started)
        if remaining <= 0:
            self._slots.release()
            raise asyncio.TimeoutError()
        control = _Request()
        worker = asyncio.create_task(asyncio.to_thread(self._send, body, control))
        def finished(task):
            self._slots.release()
            if not task.cancelled():
                task.exception()
        worker.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), remaining)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            control.cancel()
            raise
