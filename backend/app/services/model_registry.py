MODEL_REGISTRY = {
    "gpt2": {
        "architecture": "gpt2",
        "num_hidden_layers": 12,
        "num_attention_heads": 12,
        "hidden_size": 768,
        "intermediate_size": 3072,
        "vocab_size": 50257,
        "edge_min_free_gpu_mem_mb": 1024,
        "cloud_min_free_gpu_mem_mb": 1024,
    },
    "tinyllama": {
        "architecture": "llama",
        "num_hidden_layers": 22,
        "num_attention_heads": 32,
        "hidden_size": 2048,
        "intermediate_size": 5632,
        "vocab_size": 32000,
        "edge_min_free_gpu_mem_mb": 4096,
        "cloud_min_free_gpu_mem_mb": 4096,
    },
    "llama-3.2-3b": {
        "architecture": "llama",
        "num_hidden_layers": 28,
        "num_attention_heads": 24,
        "hidden_size": 3072,
        "intermediate_size": 8192,
        "vocab_size": 128256,
        "edge_min_free_gpu_mem_mb": 12288,
        "cloud_min_free_gpu_mem_mb": 12288,
    },
    "llama-3.2-3b-instruct": {
        "architecture": "llama",
        "num_hidden_layers": 28,
        "num_attention_heads": 24,
        "hidden_size": 3072,
        "intermediate_size": 8192,
        "vocab_size": 128256,
        "edge_min_free_gpu_mem_mb": None,
        "cloud_min_free_gpu_mem_mb": None,
    },
}


MODEL_CANONICAL_NAMES = {
    "gpt2": "gpt2",
    "tinyllama": "tinyllama",
    "llama-3.2-3b": "Llama-3.2-3b",
    "llama-3.2-3b-instruct": "Llama-3.2-3B-Instruct",
}

MODEL_RUNTIME_NAMES = {
    "gpt2": "gpt2",
    "tinyllama": "tinyllama",
    "llama-3.2-3b": "Llama-3.2-3B",
    "llama-3.2-3b-instruct": "Llama-3.2-3B-Instruct",
}


def resolve_model_type_key(model_type: str) -> str | None:
    normalized = (model_type or "").strip().lower()
    return normalized if normalized in MODEL_REGISTRY else None


def canonicalize_model_type(model_type: str) -> str:
    model_type_key = resolve_model_type_key(model_type)
    if model_type_key is None:
        return (model_type or "").strip()
    return MODEL_CANONICAL_NAMES.get(model_type_key, model_type_key)


def runtime_model_type(model_type: str) -> str:
    model_type_key = resolve_model_type_key(model_type)
    if model_type_key is None:
        return (model_type or "").strip()
    return MODEL_RUNTIME_NAMES.get(model_type_key, model_type_key)
