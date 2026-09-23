"""Ollama transport with no proxy, redirects, cloud model or automatic pull."""
import json
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.services.local_llm import LocalLLMError


class OllamaClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        # Validate even when a caller supplies Settings.model_construct/copy.
        validated = Settings.loopback_only(settings.ollama_base_url)
        url = urlsplit(validated)
        host = "[::1]" if url.hostname == "::1" else "127.0.0.1"
        self.base_url = f"http://{host}" + (f":{url.port}" if url.port is not None else "")
        self.transport = transport

    @staticmethod
    def _post(client: httpx.Client, path: str, body: dict) -> dict:
        # Bound response size before parsing; never expose the server's body in errors.
        with client.stream("POST", path, json=body) as response:
            if response.status_code == 404:
                raise LocalLLMError("llm_model_missing", "Локальная модель или endpoint Ollama не найдены. Подготовьте модель заранее.")
            if response.status_code != 200:
                raise LocalLLMError("llm_request_failed", "Локальный сервер отклонил запрос. Проверьте модель, размер контекста и настройки.")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise LocalLLMError("llm_response_too_large", "Ответ локальной модели слишком велик.", 502)
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError()
            return value
        except ValueError:
            raise LocalLLMError("llm_invalid_response", "Локальный сервер вернул некорректный ответ.", 502) from None

    def generate_json(self, *, system: str, user: str, schema: dict) -> str:
        model = self.settings.ollama_model.strip()
        if not model:
            raise LocalLLMError("llm_not_configured", "Задайте HACKALEM_OLLAMA_MODEL — имя заранее установленной локальной модели.")
        if "cloud" in model.casefold() or "://" in model:
            raise LocalLLMError("llm_cloud_forbidden", "Для обработки встреч разрешены только локальные модели.")
        try:
            with httpx.Client(base_url=self.base_url, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(self.settings.llm_timeout_seconds, connect=5),
                              transport=self.transport) as client:
                # No meeting data in the preflight. Detect cloud aliases as well as names.
                info = self._post(client, "/api/show", {"model": model})
                if info.get("remote_model") or info.get("remote_host") or not isinstance(info.get("model_info"), dict) or not info["model_info"]:
                    raise LocalLLMError("llm_local_model_unverified", "Ollama не подтвердила локальные веса модели; текст встречи не отправлен.")
                model_info = info["model_info"]
                limits = [v for k, v in model_info.items() if k.endswith(".context_length") and isinstance(v, int) and v > 0]
                context = min([self.settings.llm_num_ctx, *limits])
                # Conservative byte-based budget, including server template and output reserve.
                rendered = system + user + json.dumps(schema, ensure_ascii=False) + str(info.get("template", "")) + str(info.get("system", "")) + json.dumps(info.get("messages", []), ensure_ascii=False)
                if len(rendered.encode("utf-8")) + self.settings.llm_num_predict + 512 > context:
                    raise LocalLLMError("llm_context_exceeded", "Транскрипт не помещается в контекст модели. Увеличьте локальный контекст; текст не обрезается.", 413)
                reply = self._post(client, "/api/chat", {
                    "model": model, "stream": False, "format": schema,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "think": False, "truncate": False, "shift": False,
                    "options": {"temperature": 0, "num_ctx": context, "num_predict": self.settings.llm_num_predict},
                })
                if reply.get("remote_model") or reply.get("remote_host"):
                    raise LocalLLMError("llm_cloud_forbidden", "Сервер сообщил об облачном inference. Проверьте OLLAMA_NO_CLOUD=1 на сервере.")
                message = reply.get("message", {})
                if reply.get("done") is not True or reply.get("done_reason") != "stop" or not isinstance(message, dict) or message.get("tool_calls") or not isinstance(message.get("content"), str):
                    raise LocalLLMError("llm_incomplete_response", "Локальная модель не вернула завершённый JSON. Проверьте лимит ответа и совместимость модели.", 502)
                return message["content"]
        except httpx.TimeoutException:
            raise LocalLLMError("llm_timeout", "Превышено время ожидания локальной LLM.", 504) from None
        except httpx.HTTPError:
            raise LocalLLMError("llm_unavailable", "Не удалось связаться с Ollama на localhost. Запустите локальный сервер.") from None
