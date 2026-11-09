from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .base import ProviderResult


class GeminiProvider:
    """Google Gemini Interactions API adapter; API keys never leave the backend."""

    name = "gemini"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"

    async def generate(
        self,
        *,
        prompt: str,
        system_prompt: str = "",
        model: str = "gemini-3.8-flash",
        temperature: float = 1.0,
        max_output_tokens: int = 1024,
        timeout_seconds: float = 30,
    ) -> ProviderResult:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Gemini is selected but GEMINI_API_KEY is not set on the backend.")
        body: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "store": False,
            "generation_config": {
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            },
        }
        if system_prompt.strip():
            body["system_instruction"] = system_prompt
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        start = time.perf_counter()
        try:
            response_data = await asyncio.to_thread(self._request_json, request, timeout_seconds)
        except urllib.error.HTTPError as exc:
            # Do not surface response headers or request details; they can contain credentials.
            raise RuntimeError(f"Gemini request failed with HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError):
                raise RuntimeError("Gemini request timed out.") from None
            raise RuntimeError("Gemini could not be reached. Check the backend network connection.") from None
        latency = int((time.perf_counter() - start) * 1000)
        text = response_data.get("output_text")
        if not isinstance(text, str):
            text = self._extract_text(response_data)
        if not text:
            raise RuntimeError("Gemini returned no text output.")
        usage = response_data.get("usage") or response_data.get("usage_metadata") or {}
        return ProviderResult(
            text=text,
            provider=self.name,
            model=model,
            latency_ms=latency,
            input_tokens=usage.get("input_tokens") or usage.get("prompt_token_count"),
            output_tokens=usage.get("output_tokens") or usage.get("candidates_token_count"),
        )

    @staticmethod
    def _request_json(request: urllib.request.Request, timeout_seconds: float) -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise RuntimeError("Gemini response exceeded the 1 MB limit.")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise RuntimeError("Gemini returned an invalid response.")
        return decoded

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        pieces: list[str] = []
        for step in response.get("steps", []):
            if not isinstance(step, dict) or step.get("type") not in {"model_output", "text"}:
                continue
            content = step.get("content", step.get("text", ""))
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        pieces.append(part["text"])
        return "".join(pieces)
