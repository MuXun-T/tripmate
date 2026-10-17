import hashlib
import math
import re
from collections.abc import Sequence

import numpy as np

# shortcut: hashed n-grams and curated synonyms fit this small local corpus; upgrade to a local model when retrieval evaluation outgrows it.
EMBEDDING_MODEL = "tripmate-hash-ngram-v1"
EMBEDDING_DIMENSIONS = 384
_SYNONYMS = {
    "下雨": ("雨天", "降雨", "天气变化", "室内"),
    "雨天": ("下雨", "降雨", "天气变化", "室内"),
    "降雨": ("下雨", "雨天", "天气变化", "室内"),
    "票价": ("价格", "预算", "费用", "门票"),
    "价格": ("票价", "预算", "费用", "门票"),
    "余票": ("库存", "可售", "预约"),
    "库存": ("余票", "可售", "预约"),
    "预订": ("预约", "确认", "订单"),
    "预约": ("预订", "确认", "订单"),
    "改计划": ("重规划", "行程修改", "方案"),
    "重规划": ("改计划", "行程修改", "方案"),
    "饮食": ("过敏", "忌口", "素食", "敏感偏好"),
    "过敏": ("饮食", "忌口", "素食", "敏感偏好"),
}


def _expanded_text(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    additions = [
        synonym
        for term, synonyms in _SYNONYMS.items()
        if term in normalized
        for synonym in synonyms
    ]
    return f"{normalized} {' '.join(additions)}"


def _features(value: str) -> Sequence[tuple[str, float]]:
    features: list[tuple[str, float]] = []
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", _expanded_text(value)):
        features.append((token, 1.5))
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            features.extend((token[index : index + 1], 0.2) for index in range(len(token)))
            for size, weight in ((2, 0.8), (3, 1.0)):
                features.extend(
                    (token[index : index + size], weight)
                    for index in range(max(0, len(token) - size + 1))
                )
        else:
            features.extend((token[index : index + 3], 0.6) for index in range(max(0, len(token) - 2)))
    return features


def embed_text(value: str) -> list[float]:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float64)
    for feature, weight in _features(value):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * weight
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector.tolist()


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != EMBEDDING_DIMENSIONS or len(right) != EMBEDDING_DIMENSIONS:
        return 0.0
    left_vector = np.asarray(left, dtype=np.float64)
    right_vector = np.asarray(right, dtype=np.float64)
    left_norm, right_norm = float(np.linalg.norm(left_vector)), float(np.linalg.norm(right_vector))
    if not left_norm or not right_norm:
        return 0.0
    score = float(np.dot(left_vector, right_vector) / (left_norm * right_norm))
    return round(max(-1.0, min(1.0, score)), 8) if math.isfinite(score) else 0.0
