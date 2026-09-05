"""
Astrazione LLM multi-provider.
Cambiare provider = cambiare LLM_PROVIDER nelle env vars.
"""
import json
from typing import Any
from abc import ABC, abstractmethod

from app.core.config import settings


class LLMProvider(ABC):
    """Interfaccia comune per i provider LLM."""

    @abstractmethod
    def generate_json(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> dict[str, Any]:
        """Chiede al modello di rispondere SOLO con JSON valido."""
        ...


# ─────────────────────────────────────────────
# GEMINI
# ─────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    def __init__(self):
        import google.generativeai as genai
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY non impostata")
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._model = genai.GenerativeModel(
            model_name=settings.LLM_MODEL_GEMINI,
            generation_config={"response_mime_type": "application/json"},
        )

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_output_tokens: int = 16384,      # default alto: una lista di 20
    ) -> dict[str, Any]:                      # spettacoli non ci sta in 8k
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
        response = self._model.generate_content(
            full_prompt,
            generation_config={
                "temperature": temperature,
                "response_mime_type": "application/json",
                "max_output_tokens": max_output_tokens,
            },
        )

        # Perché non c'è testo: senza questo controllo l'errore è illeggibile
        cand = (response.candidates or [None])[0]
        if cand is not None:
            motivo = getattr(cand, "finish_reason", None)
            if motivo is not None and str(motivo).upper().endswith("MAX_TOKENS"):
                raise ValueError(
                    f"risposta troncata al tetto di {max_output_tokens} token: "
                    "spezza l'input o alza max_output_tokens"
                )
            if motivo is not None and str(motivo).upper().endswith("SAFETY"):
                raise ValueError("risposta bloccata dai filtri di sicurezza di Gemini")

        try:
            text = (response.text or "").strip()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Gemini non ha restituito testo: {e}") from e
        if not text:
            raise ValueError("Gemini ha restituito una risposta vuota")

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"JSON non valido ({len(text)} caratteri, "
                f"finisce con: …{text[-80:]!r})"
            ) from e


# ─────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────

class ClaudeProvider(LLMProvider):
    def __init__(self):
        from anthropic import Anthropic
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY non impostata")
        self._client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def generate_json(self, system_prompt: str, user_prompt: str,
                  temperature: float = 0.7,
                  max_output_tokens: int = 16384) -> dict[str, Any]:
        # Aggiungiamo al system prompt un'istruzione esplicita di rispondere SOLO JSON.
        json_instruction = "\n\nIMPORTANT: Respond ONLY with a valid JSON object. No prose, no markdown, no code fences. Just the JSON."
        full_system = system_prompt + json_instruction

        message = self._client.messages.create(
            model=settings.LLM_MODEL_CLAUDE,
            max_tokens=max_output_tokens,
            temperature=temperature,
            system=full_system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = message.content[0].text.strip()

        # Pulizia: rimuovi eventuali fences di markdown
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Claude didn't return valid JSON. Got: {text[:500]}") from e


# ─────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────

_provider_cache: LLMProvider | None = None


def get_llm() -> LLMProvider:
    """Ritorna il provider configurato. Cached per non re-init ogni request."""
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache

    provider_name = settings.LLM_PROVIDER.lower()
    if provider_name == "gemini":
        _provider_cache = GeminiProvider()
    elif provider_name == "claude":
        _provider_cache = ClaudeProvider()
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER: {provider_name}")
    return _provider_cache


def reset_llm_cache():
    """Forza re-init (utile per test)."""
    global _provider_cache
    _provider_cache = None