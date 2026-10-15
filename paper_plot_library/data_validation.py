"""Read-only, offline preflight for the three checked-in collection state files."""
from pathlib import Path
import json
import math
import re

from .pipeline import validate_catalog


class DataValidationError(ValueError):
    """An actionable data-file error, without exposing file contents or secrets."""


class _StrictJSONError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("JSON 含重复字段，不能确定应使用哪一个值")
        result[key] = value
    return result


def _reject_constant(_value):
    raise _StrictJSONError("JSON 不允许 NaN 或 Infinity 非有限数值")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _StrictJSONError("JSON 数值溢出为非有限数值")
    return number


def _error(label, text, position, message):
    line = text.count("\n", 0, position) + 1
    column = position - text.rfind("\n", 0, position)
    return DataValidationError(f"{label}:{line}:{column}: {message}")


def _read_strict_json(root, relative):
    path = root / relative
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DataValidationError(f"{relative}:1:1: 文件缺失、不可读或不是 UTF-8") from exc
    # Never try to choose a side of a merge while validating publication data.
    marker = re.search(r"(?m)^[ \t]*(?:<{7}(?:[ \t].*)?|={7}|>{7}(?:[ \t].*)?|\|{7}(?:[ \t].*)?)[ \t]*\r?$", text)
    if marker:
        raise _error(relative, text, marker.start(), "存在未解决的 Git 合并冲突；请先修复，不要直接重新运行")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                          parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise _error(relative, text, exc.pos, "JSON 格式无效（" + exc.msg + "）") from exc
    except (_StrictJSONError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, _StrictJSONError) else "JSON 嵌套过深，无法安全校验"
        raise _error(relative, text, 0, message) from exc


def validate_data(root):
    """Validate real state without settings, API calls, locks, or file writes.

    Ledger extras are intentionally preserved and tolerated: this is not a
    quota migration, expiry pass, or collection run.
    """
    root = Path(root)
    catalog = _read_strict_json(root, "data/catalog.json")
    ledger = _read_strict_json(root, "data/ledger.json")
    updates = _read_strict_json(root, "data/updates.json")
    try:
        validate_catalog(catalog)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DataValidationError("data/catalog.json:1:1: 案例库结构无效（" + str(exc) + "）") from exc
    if not isinstance(ledger, dict):
        raise DataValidationError("data/ledger.json:1:1: 配额记录顶层必须为 JSON 对象")
    if not isinstance(updates, list):
        raise DataValidationError("data/updates.json:1:1: 运行记录顶层必须为 JSON 列表")
    return {"status": "valid", "files": ["data/catalog.json", "data/ledger.json", "data/updates.json"],
            "papers": len(catalog["papers"]), "updateRecords": len(updates),
            "httpRequests": 0, "aiAttempts": 0, "filesWritten": 0}
