"""
llm.py — Pluggable model backend
==================================
Drop-in replacement for Ollama in pipeline.py.

Provides two public functions that match the calling convention of the
old _call_ollama_sync / _call_ollama_stream:

    llm_generate(prompt, max_tokens)  → str
    llm_stream(prompt, max_tokens)    → generator[str]

Backend is selected by AI_MODE in config.py (or the AI_MODE env var):

    "local"   Qwen2.5-0.5B-Instruct via HuggingFace transformers.
              Loads once at startup, ~1.2 GB RAM, no HTTP dependency.
              Falls back to "mock" if transformers is not installed.

    "ollama"  Original Ollama HTTP path — kept so you can switch back
              by setting AI_MODE=ollama without any code changes.

    "mock"    Instant deterministic output — useful for CI, tests, and
              development when you don't want to wait for a real model.

Adding a new backend
────────────────────
1. Add a new elif branch in _get_backend() that returns a Backend subclass.
2. Implement generate() and stream() on that subclass.
3. Set AI_MODE=<your_backend_name> in the environment.
No changes to pipeline.py needed.

Config keys (all in config.py, all env-overridable)
────────────────────────────────────────────────────
    AI_MODE             "local" | "ollama" | "mock"   default: "local"
    LOCAL_MODEL_NAME    HuggingFace model id           default: "Qwen/Qwen2.5-0.5B-Instruct"
    LOCAL_MODEL_DEVICE  "cpu" | "cuda" | "mps"         default: "cpu"
    LOCAL_MODEL_TEMP    float, generation temperature  default: 0.15
"""

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, cast

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# ABSTRACT BACKEND
# ══════════════════════════════════════════════════════════════════════════════

class Backend(ABC):
    """Base class — every backend implements generate() and stream()."""

    @abstractmethod
    def generate(self, prompt: str, max_tokens: int) -> str:
        """Return the full response as a string."""

    @abstractmethod
    def stream(self, prompt: str, max_tokens: int) -> Iterator[str]:
        """Yield response tokens one at a time."""


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND 1 — LOCAL (Qwen2.5-0.5B-Instruct via transformers)
# ══════════════════════════════════════════════════════════════════════════════

class LocalBackend(Backend):
    """
    HuggingFace transformers backend.

    Model loads once on first call (lazy) and is reused across all requests.
    A threading.Lock prevents concurrent loads on multi-threaded Flask workers.

    Streaming is implemented via TextIteratorStreamer which runs generation
    in a background thread and yields tokens as they are produced — the
    same pattern used by the HuggingFace streaming docs.
    """

    def __init__(self, model_name: str, device: str, temperature: float):
        self.model_name  = model_name
        self.device      = device
        self.temperature = temperature
        self._pipe       = None
        self._lock       = threading.Lock()
        self._load_error: str | None = None

    def _get_pipe(self):
        """Load the pipeline once. Returns None on failure."""
        if self._pipe is not None:
            return self._pipe
        if self._load_error:
            return None

        with self._lock:
            if self._pipe is not None:
                return self._pipe
            try:
                from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline as hf_pipeline
                import torch

                logger.info(
                    "[LLM] Loading %s on %s …", self.model_name, self.device
                )
                t0 = time.perf_counter()
                # Load tokenizer separately so we can use it in stream()
                tokenizer = AutoTokenizer.from_pretrained(
                    self.model_name,
                    trust_remote_code=True,
                )
                # Load model — use 4-bit quantization if bitsandbytes available
                model_kwargs: dict[str, Any] = {
                    "trust_remote_code": True,
                    "low_cpu_mem_usage": True,
                }
                try:
                    from transformers import BitsAndBytesConfig
                    bnb_config = BitsAndBytesConfig(load_in_4bit=True)
                    model_kwargs["quantization_config"] = bnb_config
                    logger.info("[LLM] 4-bit quantization enabled via bitsandbytes")
                except ImportError:
                    logger.info("[LLM] bitsandbytes not available — loading in float32")

                model = AutoModelForCausalLM.from_pretrained(
                    self.model_name,
                    **model_kwargs,
                )
                model.eval()

                self._pipe = hf_pipeline(
                    "text-generation",
                    model      = model,
                    tokenizer=tokenizer,
                    device_map="auto" if self.device != "cpu" else None,
                    # Keep the pipeline lean — no sampling overhead at load time
                )
                elapsed = round((time.perf_counter() - t0), 1)
                logger.info("[LLM] Model loaded in %.1fs", elapsed)

            except ImportError:
                self._load_error = (
                    "transformers / torch not installed: {exc} "
                )
                logger.error("[LLM] %s", self._load_error)
            except Exception as exc:
                self._load_error = str(exc)
                logger.error("[LLM] Failed to load model: %s", exc)

        return self._pipe

    def _build_messages(self, prompt: str) -> list[dict]:
        """
        Wrap the raw prompt in the chat-message format that
        Qwen2.5-Instruct (and most instruct models) expect.

        The system message instructs the model to stay factual and concise —
        critical for an HR reporting context where hallucinated numbers
        are harmful.
        """
        return [
            {
                "role": "system",
                "content": (
                    "You are a precise HR data reporter for IIT Bombay "
                    "Nanofabrication Facility. "
                    "Output only facts from the provided data. "
                    "Do not invent numbers, dates, or names. "
                    "Do not add disclaimers, greetings, or preambles."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]

    def generate(self, prompt: str, max_tokens: int = 500) -> str:
        pipe = self._get_pipe()
        if pipe is None:
            return f"[ERROR] Model unavailable: {self._load_error}"

        try:
            t0 = time.perf_counter()
            messages = self._build_messages(prompt)
            result = pipe(
                messages,
                max_new_tokens = max_tokens,
                temperature    = self.temperature,
                do_sample      = self.temperature > 0,
                top_p          = 0.9,
                repetition_penalty = 1.1,
                # Don't return the input prompt in the output
                return_full_text = False,
            )
            # transformers pipeline returns a list of dicts
            # result[0]["generated_text"] is a list of message dicts
            # the last one is the assistant reply
            generated = result[0]["generated_text"]
            if isinstance(generated, list):
                # Chat format: list of {"role": ..., "content": ...}
                text = generated[-1].get("content", "")
            else:
                text = str(generated)

            elapsed = round((time.perf_counter() - t0) * 1000)
            logger.info("[LLM] generate: %d tokens in %dms", max_tokens, elapsed)
            return text.strip()
        

        except Exception as exc:
            logger.error("[LLM] generate() failed: %s", exc)
            return f"[ERROR] Generation failed: {exc}"
        

    def stream(self, prompt: str, max_tokens: int = 500):
        """
        True token streaming via TextIteratorStreamer.

        Generation runs in a daemon thread. Tokens are placed into the
        streamer queue and yielded here as they arrive — no buffering,
        no blocking the Flask SSE response.
        """
        pipe = self._get_pipe()
        if pipe is None:
            yield f"[ERROR] Model unavailable: {self._load_error}"
            return

        try:
            from transformers import TextIteratorStreamer
            import threading

            messages = self._build_messages(prompt)
            tokenizer = pipe.tokenizer

            inputs = tokenizer.apply_chat_template(
                messages,
                tokenize         = True,
                add_generation_prompt = True,
                return_tensors   = "pt",
            )

            streamer = TextIteratorStreamer(
                tokenizer,
                skip_prompt           = True,   # don't echo the input
                skip_special_tokens   = True,
                clean_up_tokenization_spaces = True,
            )
            input_ids = inputs.to(pipe.model.device)  # ensure correct device
            gen_kwargs = {
                "inputs":           input_ids,
                "max_new_tokens":      max_tokens,
                "temperature":         self.temperature,
                "do_sample":           self.temperature > 0,
                "top_p":               0.9,
                "repetition_penalty":  1.1,
                "streamer":            streamer,
            }

            # Run generation in a background thread so we can yield tokens
            # from the main thread without blocking the SSE response.

            gen_thread = threading.Thread(
                target  = pipe.model.generate,
                kwargs  = gen_kwargs,
                daemon  = True,
            )
            gen_thread.start()

            # Yield tokens as they arrive from the streamer queue
            for token in streamer:
                if token:
                    yield token

            gen_thread.join(timeout=120)

        except ImportError:
            # TextIteratorStreamer not available — fall back to word-by-word
            # from the full generate() output
            logger.warning("[LLM] TextIteratorStreamer unavailable — using word stream fallback")
            full = self.generate(prompt, max_tokens)
            for word in full.split(" "):
                yield word + " "

        except Exception as exc:
            logger.error("[LLM] stream() failed: %s", exc)
            yield f"[ERROR] Streaming failed: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND 2 — OLLAMA (original HTTP path, kept as fallback)
# ══════════════════════════════════════════════════════════════════════════════

class OllamaBackend(Backend):
    """
    Original Ollama HTTP backend — preserved so AI_MODE=ollama still works.
    Identical logic to the old _call_ollama_sync / _call_ollama_stream.
    """

    def __init__(self, url: str, model: str, temperature: float = 0.4):
        self.url         = url
        self.model       = model
        self.temperature = temperature

    def generate(self, prompt: str, max_tokens: int = 500) -> str:
        import requests
        payload = {
            "model":   self.model,
            "prompt":  prompt,
            "stream":  False,
            "options": {
                "num_predict": max_tokens,
                "temperature": self.temperature,
                "top_p":       0.9,
            },
        }
        try:
            resp = requests.post(
                f"{self.url}/api/generate",
                json    = payload,
                timeout = 120,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except requests.exceptions.ConnectionError:
            logger.error("[LLM/Ollama] Not reachable at %s", self.url)
            return ""
        except Exception as exc:
            logger.error("[LLM/Ollama] generate() failed: %s", exc)
            return ""

    def stream(self, prompt: str, max_tokens: int = 500):
        import json as _json
        import requests
        payload = {
            "model":   self.model,
            "prompt":  prompt,
            "stream":  True,
            "options": {
                "num_predict": max_tokens,
                "temperature": self.temperature,
                "top_p":       0.9,
            },
        }
        try:
            resp = requests.post(
                f"{self.url}/api/generate",
                json    = payload,
                stream  = True,
                timeout = 120,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            logger.error("[LLM/Ollama] Not reachable at %s", self.url)
            yield "[ERROR] Ollama is not running. Run: ollama serve"
            return
        except Exception as exc:
            logger.error("[LLM/Ollama] stream() failed: %s", exc)
            yield f"[ERROR] {exc}"
            return

        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = _json.loads(line)
                token = chunk.get("response", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break
            except _json.JSONDecodeError:
                continue


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND 3 — MOCK (instant deterministic output for testing / CI)
# ══════════════════════════════════════════════════════════════════════════════

class MockBackend(Backend):
    """
    Returns a canned response instantly — zero latency, zero dependencies.
    Useful for:
      - Unit tests that import pipeline.py
      - Development without a GPU or internet connection
      - CI pipelines where model inference is not needed
    Set AI_MODE=mock to activate.
    """

    _RESPONSE = (
        "John Doe holds the designation of Research Engineer within the "
        "Nanofabrication team at IITBNF. They joined on 2021-06-15 on a "
        "Project appointment basis.\n\n"
        "Attendance stands at 88.5% for the current year (212 days present "
        "out of 239), meeting the mandatory 75% threshold. A total of 12 "
        "leave days have been recorded this year.\n\n"
        "35 equipment usage requests have been submitted, of which 28 have "
        "been slot-booked. Tool access permissions are held for 11 pieces of "
        "equipment. Currently assigned as system owner for 3 tools.\n\n"
        "2 approved research publications are on record, associated with 3 "
        "faculty projects of which 2 are currently active."
    )

    def generate(self, prompt: str, max_tokens: int = 500) -> str:
        return self._RESPONSE

    def stream(self, prompt: str, max_tokens: int = 500):
        for word in self._RESPONSE.split(" "):
            yield word + " "
            time.sleep(0.01)   # simulate token latency so SSE works correctly

class LlamaCppBackend(Backend):
    """
    llama-cpp-python backend — runs GGUF quantized models on CPU.
    Q4_K_M gives ~350MB RAM vs ~1GB for float32 transformers.
    Significantly faster on CPU than transformers float32.
    """

    def __init__(self, model_path: str, temperature: float = 0.15):
        self.model_path  = model_path
        self.temperature = temperature
        self._llm        = None
        self._lock       = threading.Lock()
        self._load_error = None

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        if self._load_error:
            return None
        with self._lock:
            if self._llm is not None:
                return self._llm
            try:
                from llama_cpp import Llama
                import os as _os
                if not _os.path.exists(self.model_path):
                    self._load_error = f"GGUF file not found: {self.model_path}"
                    logger.error("[LLM/llama.cpp] %s", self._load_error)
                    return None
                logger.info("[LLM/llama.cpp] Loading %s", self.model_path)
                t0 = time.perf_counter()
                self._llm = Llama(
                    model_path    = self.model_path,
                    n_ctx         = 2048,
                    n_threads     = 4,        # adjust to your CPU core count
                    n_gpu_layers  = 0,        # 0 = CPU only
                    verbose       = False,
                )
                elapsed = round(time.perf_counter() - t0, 1)
                logger.info("[LLM/llama.cpp] Loaded in %.1fs", elapsed)
            except ImportError:
                self._load_error = "llama-cpp-python not installed"
                logger.error("[LLM/llama.cpp] %s", self._load_error)
            except Exception as exc:
                self._load_error = str(exc)
                logger.error("[LLM/llama.cpp] Load failed: %s", exc)
        return self._llm

    def _system_prompt(self):
        return (
            "You are a precise HR data reporter for IIT Bombay "
            "Nanofabrication Facility. Output only facts from the "
            "provided data. Do not invent numbers, dates, or names. "
            "Do not add disclaimers, greetings, or preambles."
        )

    def generate(self, prompt: str, max_tokens: int = 500) -> str:
        tag = f"pid={os.getpid()} tid={threading.get_ident()}"
        logger.info("[LLM] %s START generate", tag)
        t0 = time.perf_counter()
        llm = self._get_llm()
        if llm is None:
            return f"[ERROR] Model unavailable: {self._load_error}"
        try:
            result = llm.create_chat_completion(
                messages=[
                    {"role": "system",  "content": self._system_prompt()},
                    {"role": "user",    "content": prompt},
                ],
                max_tokens  = max_tokens,
                temperature = self.temperature,
                top_p       = 0.9,
                repeat_penalty = 1.1,
            )
            elapsed = round((time.perf_counter() - t0), 1)
            logger.info("[LLM] %s END generate (%.1fs)", tag, elapsed)
            return result["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.error("[LLM/llama.cpp] generate() failed: %s", exc)
            return f"[ERROR] Generation failed: {exc}"

    def stream(self, prompt: str, max_tokens: int = 500):
        tag = f"pid={os.getpid()} tid={threading.get_ident()}"
        logger.info("LLM %s START stream", tag)
        t0 = time.perf_counter()
        llm = self._get_llm()
        if llm is None:
            yield f"[ERROR] Model unavailable: {self._load_error}"
            return
        try:
            stream = llm.create_chat_completion(
                messages=[
                    {"role": "system",  "content": self._system_prompt()},
                    {"role": "user",    "content": prompt},
                ],
                max_tokens  = max_tokens,
                temperature = self.temperature,
                top_p       = 0.9,
                repeat_penalty = 1.1,
                stream      = True,
            )
            for chunk in stream:
                delta = chunk["choices"][0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    yield token
        except Exception as exc:
            logger.error("[LLM/llama.cpp] stream() failed: %s", exc)
            yield f"[ERROR] Streaming failed: {exc}"
        finally:
            elapsed = round((time.perf_counter() - t0), 1)
            logger.info("[LLM] %s END stream (%.1fs)", tag, elapsed)
# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON FACTORY
# ══════════════════════════════════════════════════════════════════════════════

_backend: Backend | None  = None
_backend_lock             = threading.Lock()


def _get_backend() -> Backend:
    """
    Return the active backend singleton, creating it on first call.

    Selection order:
      1. AI_MODE env var / config.py
      2. Fall back to "mock" if "local" is requested but transformers missing
    """
    global _backend
    if _backend is not None:
        return _backend

    with _backend_lock:
        if _backend is not None:
            return _backend

        try:
            import config as _cfg
            # Read AI_MODE directly from env so tests and runtime config changes work
            mode        = (os.getenv("AI_MODE") or getattr(_cfg, "AI_MODE", "local")).lower().strip()
            model_name  = os.getenv("LOCAL_MODEL_NAME")  or getattr(_cfg, "LOCAL_MODEL_NAME",   "Qwen/Qwen2.5-0.5B-Instruct")
            device      = os.getenv("LOCAL_MODEL_DEVICE") or getattr(_cfg, "LOCAL_MODEL_DEVICE",  "cpu")
            temperature = float(os.getenv("LOCAL_MODEL_TEMP") or getattr(_cfg, "LOCAL_MODEL_TEMP", 0.15))
            ollama_url  = os.getenv("OLLAMA_URL")   or getattr(_cfg, "OLLAMA_URL",   "http://localhost:11434")
            ollama_model= os.getenv("OLLAMA_MODEL") or getattr(_cfg, "OLLAMA_MODEL", "llama3.2")
        except ImportError:
            mode        = (os.getenv("AI_MODE") or "local").lower().strip()
            model_name  = os.getenv("LOCAL_MODEL_NAME",   "Qwen/Qwen2.5-0.5B-Instruct")
            device      = os.getenv("LOCAL_MODEL_DEVICE", "cpu")
            temperature = float(os.getenv("LOCAL_MODEL_TEMP", "0.15"))
            ollama_url  = os.getenv("OLLAMA_URL",   "http://localhost:11434")
            ollama_model= os.getenv("OLLAMA_MODEL", "llama3.2")

        if mode == "ollama":
            _backend = OllamaBackend(ollama_url, ollama_model, temperature=0.4)

        elif mode == "mock":
            _backend = MockBackend()
            logger.info("[LLM] Mock backend active — instant responses")
        elif mode == "llamacpp":
            gguf_path = os.getenv("GGUF_MODEL_PATH") or getattr(_cfg, "GGUF_MODEL_PATH", "qwen2.5-0.5b-instruct.Q4_K_M.gguf")
            _backend  = LlamaCppBackend(gguf_path, temperature=temperature)
            logger.info("[LLM] llama.cpp backend: %s", gguf_path)
        else:
            # "local" or any unrecognised value — attempt transformers
            _backend = LocalBackend(model_name, device, temperature)
            logger.info(
                "[LLM] Local backend: model=%s  device=%s  temp=%.2f",
                model_name, device, temperature,
            )

    return _backend


def warm_up() -> None:
    """
    Pre-load the model at server startup so the first real request
    doesn't pay the load cost. Called from app.py _startup_tasks().
    Non-fatal — a failure here just means the first request is slower.
    """
    try:
        backend = _get_backend()
        if isinstance(backend, LocalBackend):
            backend._get_pipe()   # triggers the actual model download/load
            logger.info("[LLM] Model pre-warmed successfully")
        elif isinstance(backend, LlamaCppBackend):
            backend._get_llm()
            logger.info("[LLM] llama.cpp model pre-warmed")
        else:
            logger.info("[LLM] Backend %s does not need pre-warming", type(backend).__name__)
    except Exception as exc:
        logger.warning("[LLM] warm_up() failed (non-fatal): %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — drop-in for _call_ollama_sync / _call_ollama_stream
# ══════════════════════════════════════════════════════════════════════════════

def llm_generate(prompt: str, max_tokens: int = 500) -> str:
    """
    Non-streaming generation. Returns the full response as a string.
    Drop-in replacement for _call_ollama_sync().
    """
    return _get_backend().generate(prompt, max_tokens)


def llm_stream(prompt: str, max_tokens: int = 500):
    """
    Streaming generation. Yields string tokens one at a time.
    Drop-in replacement for _call_ollama_stream().
    """
    yield from _get_backend().stream(prompt, max_tokens)
# Add to llm.py after the _get_backend() factory

def is_llm_available() -> bool:
    """
    Quick non-blocking check: is the SLM loaded and ready?
    Returns False if the backend failed to load or is unavailable.
    """
    try:
        backend = _get_backend()
        if isinstance(backend, MockBackend):
            return False  # Mock = no real SLM
        if isinstance(backend, LlamaCppBackend):
            return backend._llm is not None and backend._load_error is None
        if isinstance(backend, LocalBackend):
            return backend._pipe is not None and backend._load_error is None
        if isinstance(backend, OllamaBackend):
            # Quick connectivity check
            import requests
            try:
                requests.get(f"{backend.url}/api/tags", timeout=1)
                return True
            except Exception:
                return False
        return False
    except Exception:
        return False
    
from app import app
with app.app_context():
    from llm import is_llm_available, warm_up
    warm_up()
    print(is_llm_available())   # must print True