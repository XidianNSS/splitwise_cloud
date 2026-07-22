MODEL_REGISTRY = {
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
        "edge_min_free_gpu_mem_mb": 12288,
        "cloud_min_free_gpu_mem_mb": 12288,
    },
    "bert-base-uncased": {
        "architecture": "bert",
        "capability": "embeddings",
        "deployment_mode": "encrypted",
        "strategy_kind": "fixed_bert_encoder",
        "num_hidden_layers": 12,
        "num_attention_heads": 12,
        "hidden_size": 768,
        "intermediate_size": 3072,
        "vocab_size": 30522,
        "edge_min_free_gpu_mem_mb": 1024,
        "cloud_min_free_gpu_mem_mb": 1024,
    },
}


MODEL_CANONICAL_NAMES = {
    "llama-3.2-3b": "Llama-3.2-3b",
    "llama-3.2-3b-instruct": "Llama-3.2-3B-Instruct",
    "bert-base-uncased": "BERT-Base-Uncased",
}

MODEL_RUNTIME_NAMES = {
    "llama-3.2-3b": "Llama-3.2-3B",
    "llama-3.2-3b-instruct": "Llama-3.2-3B-Instruct",
    "bert-base-uncased": "BERT-Base-Uncased",
}


def list_model_catalog() -> list[dict]:
    """Return the public model capabilities supported by the scheduler."""
    return [
        {
            "model_type": MODEL_CANONICAL_NAMES.get(key, key),
            "runtime_model_type": MODEL_RUNTIME_NAMES.get(key, key),
            "architecture": spec["architecture"],
            "capability": spec.get("capability", "generation"),
            "deployment_mode": spec.get("deployment_mode", "standard"),
            "strategy_kind": spec.get("strategy_kind", "algorithm"),
        }
        for key, spec in MODEL_REGISTRY.items()
    ]


def uses_fixed_runtime_strategy(model_type_key: str) -> bool:
    return (
        MODEL_REGISTRY[model_type_key].get("strategy_kind", "algorithm")
        != "algorithm"
    )


def build_fixed_runtime_decision(model_type: str, model_type_key: str) -> dict:
    """Build the deterministic runtime contract for non-generative BERT."""
    spec = MODEL_REGISTRY[model_type_key]
    if spec.get("strategy_kind") != "fixed_bert_encoder":
        raise ValueError(f"模型不使用固定 runtime 策略: {model_type}")
    return {
        "model_type": model_type,
        "strategy_kind": "fixed_bert_encoder",
        "capability": "embeddings",
        "deployment_mode": spec.get("deployment_mode", "encrypted"),
        "layer_partitions": [
            {
                "layer_id": layer_id,
                "head_assignments": [1] * spec["num_attention_heads"],
                "ffn_assignment": 1,
                "edge_head_count": 0,
                "cloud_head_count": spec["num_attention_heads"],
            }
            for layer_id in range(spec["num_hidden_layers"])
        ],
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
