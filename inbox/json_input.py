import json
import math


def _reject_constant(value):
    raise ValueError("Non-finite numbers are not valid JSON.")


def load_json(body):
    value = json.loads(body, parse_constant=_reject_constant)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite numbers are not valid JSON.")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value
