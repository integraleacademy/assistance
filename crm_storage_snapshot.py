"""Isolated JSON snapshots that reuse unchanged, read-only cached subtrees."""
import copy


_MISSING = object()


def storage_snapshot(value, previous=_MISSING):
    """Copy mutable input, sharing only objects from the previous snapshot.

    Callers still receive independent mutable data from load_data(). On save,
    unchanged contact histories need not be allocated for a third time. Neither
    the previous snapshot nor the caller's containers are ever modified here.
    """
    if type(value) is dict:
        old = previous if type(previous) is dict else {}
        result = {key: storage_snapshot(item, old.get(key, _MISSING))
                  for key, item in value.items()}
        if (type(previous) is dict and list(result) == list(old)
                and all(item is old[key] for key, item in result.items())):
            return previous
        return result
    if type(value) is list:
        old = previous if type(previous) is list else []
        result = [storage_snapshot(item, old[i] if i < len(old) else _MISSING)
                  for i, item in enumerate(value)]
        if (type(previous) is list and len(result) == len(old)
                and all(item is old[i] for i, item in enumerate(result))):
            return previous
        return result
    if type(value) is float:
        # Preserve signed zero as well as the exact serialized numeric type.
        return previous if type(previous) is float and value.hex() == previous.hex() else value
    if type(value) in (str, int, bool, type(None)):
        # bool/int and int/float compare equal in Python but differ in JSON.
        return previous if type(value) is type(previous) and value == previous else value
    # Preserve the legacy deepcopy contract for other serializable Python types.
    return copy.deepcopy(value)
