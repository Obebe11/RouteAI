import json

import httpx


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, default_api_key: str):
        self._default_api_key = default_api_key

    def _resolve_key(self, api_key: str | None) -> str:
        return api_key or self._default_api_key

    @staticmethod
    def _auth_headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async def list_free_models(self, api_key: str | None = None) -> list[dict]:
        """Return sorted list of free models with id, name, context_length."""
        key = self._resolve_key(api_key)
        try:
            async with httpx.AsyncClient(
                timeout=30.0, headers=self._auth_headers(key)
            ) as client:
                resp = await client.get("https://openrouter.ai/api/v1/models")
        except httpx.HTTPError as exc:
            raise OpenRouterError(str(exc)) from exc

        if resp.status_code != 200:
            raise OpenRouterError(f"{resp.status_code}: {resp.text}")

        data = resp.json().get("data", [])
        free = []
        for m in data:
            # ЗАЩИТА ОТ СПИСАНИЯ ДЕНЕГ. Цена в API ненадёжна: preview/медиа-модели
            # (напр. google/lyria-*) показывают нулевые prompt/completion, но
            # реально тарифицируют генерацию аудио/картинок по скрытым полям.
            pricing = m.get("pricing") or {}
            nonzero = any(
                str(v) not in ("0", "0.0") for v in pricing.values() if v is not None
            )
            # openrouter/free показываем отдельной закреплённой кнопкой
            # «Случайная модель», в общий список не дублируем.
            if m.get("id") == "openrouter/free":
                continue
            out = set((m.get("architecture") or {}).get("output_modalities") or ["text"])
            is_free_suffix = m.get("id", "").endswith(":free")

            if is_free_suffix:
                pass  # суффикс :free — явная гарантия OpenRouter, цену не проверяем
            else:
                # Без ":free" пускаем ТОЛЬКО чисто текстовые модели с нулевой ценой
                # (owl-alpha и подобные cloaked-превью). Любой медиа-вывод
                # (audio/image/video) = потенциальная плата → исключаем.
                if out - {"text"} or nonzero:
                    continue
            free.append(m)

        result = [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "context_length": m.get("context_length", 0),
                "output_modalities": (m.get("architecture") or {}).get(
                    "output_modalities"
                )
                or ["text"],
                "input_modalities": (m.get("architecture") or {}).get(
                    "input_modalities"
                )
                or ["text"],
            }
            for m in free
        ]
        result.sort(key=lambda x: x["name"])
        return result

    async def validate_key(self, api_key: str) -> bool:
        """True, если ключ принимается OpenRouter (GET /api/v1/key → 200)."""
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers={"Authorization": f"Bearer {api_key}"}
            ) as client:
                resp = await client.get("https://openrouter.ai/api/v1/key")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def free_endpoint_uptime(self, model_id: str, api_key: str | None = None) -> float | None:
        """Uptime (%) бесплатного эндпоинта модели за сутки, либо None если неизвестно."""
        key = self._resolve_key(api_key)
        try:
            async with httpx.AsyncClient(
                timeout=20.0, headers=self._auth_headers(key)
            ) as client:
                resp = await client.get(
                    f"https://openrouter.ai/api/v1/models/{model_id}/endpoints"
                )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or {}
        endpoints = data.get("endpoints") or []
        # У :free-варианта ровно бесплатный эндпоинт (prompt == 0). Берём первый
        # с ценой 0; если такого нет — первый доступный.
        free = next(
            (e for e in endpoints if str(e.get("pricing", {}).get("prompt")) == "0"),
            endpoints[0] if endpoints else None,
        )
        if not free:
            return None
        for field in ("uptime_last_1d", "uptime_last_30m", "uptime_last_5m"):
            val = free.get(field)
            if isinstance(val, (int, float)) and val >= 0:
                return float(val)
        return None

    async def image_generate(
        self,
        model: str,
        prompt: str,
        api_key: str | None = None,
    ) -> str:
        """Generate an image, return URL. Raises OpenRouterError on failure."""
        key = self._resolve_key(api_key)
        headers = {
            **self._auth_headers(key),
            "HTTP-Referer": "https://github.com/Obebe11/RouteAI",
            "X-Title": "RouterAi",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=90.0, headers=headers) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/images/generations",
                    json={"model": model, "prompt": prompt},
                )
        except httpx.HTTPError as exc:
            raise OpenRouterError(str(exc)) from exc
        if resp.status_code != 200:
            raise OpenRouterError(f"{resp.status_code}: {resp.text}")
        images = (resp.json().get("data") or [])
        if not images or not images[0].get("url"):
            raise OpenRouterError("No image URL in response")
        return images[0]["url"]

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        api_key: str | None = None,
        temperature: float = 0.7,
    ):
        """Yield text chunks from a streaming chat completion."""
        key = self._resolve_key(api_key)
        headers = {
            **self._auth_headers(key),
            "HTTP-Referer": "https://github.com/Obebe11/RouteAI",
            "X-Title": "RouterAi",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                async with client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise OpenRouterError(
                            f"{resp.status_code}: {body.decode(errors='replace')}"
                        )

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith(":"):
                            continue
                        if not line.startswith("data: "):
                            continue
                        payload_str = line[len("data: "):]
                        if payload_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        content = (choices[0].get("delta") or {}).get("content")
                        if content:
                            yield content
        except httpx.HTTPError as exc:
            raise OpenRouterError(str(exc)) from exc
