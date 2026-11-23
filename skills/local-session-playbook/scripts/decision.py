"""Calculate wait-time guidance from a user's observed cross-chat activity."""

from math import sqrt


WAIT_BINS = (
    ("under_30s", 0, 30, "<30 秒"),
    ("30_to_119s", 30, 120, "30 秒–2 分"),
    ("120_to_299s", 120, 300, "2–5 分"),
    ("300s_plus", 300, None, "≥5 分"),
)


def clean_buckets(source):
    """Keep only consistent aggregate counts from the documented wait bins."""
    if not isinstance(source, dict):
        return {}
    result = {}
    for key, _, _, _ in WAIT_BINS:
        bucket = source.get(key)
        if not isinstance(bucket, dict):
            continue
        total, switched = bucket.get("rounds"), bucket.get("other_chat_prompt")
        if type(total) is int and type(switched) is int and 0 <= switched <= total <= 10**12:
            result[key] = {"rounds": total, "other_chat_prompt": switched}
    return result


def wilson_interval(switched, total):
    """Use a conservative rate band as a guard against thin samples."""
    z = 1.96
    rate = switched / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * sqrt((rate * (1 - rate) + z * z / (4 * total)) / total) / denominator
    return center - margin, center + margin


def decision_cutoffs(buckets):
    """Return seconds for the supported stay-single and try-parallel boundaries."""
    bands = {}
    for key, bucket in buckets.items():
        total = bucket["rounds"]
        if total >= 20:
            bands[key] = wilson_interval(bucket["other_chat_prompt"], total)

    stay_single_before = None
    for key, _, end, _ in WAIT_BINS[:-1]:
        if key not in bands or bands[key][1] > 0.25:
            break
        stay_single_before = end

    try_parallel_at = None
    for key, start, _, _ in reversed(WAIT_BINS):
        if key not in bands:
            continue
        if bands[key][0] < 0.60:
            break
        try_parallel_at = start

    return stay_single_before, try_parallel_at
