"""Offline admission checks for LOG_DISARMED recording evidence."""


def growing_log(captures):
    """Three ordered (monotonic time, id:size mapping, END sent) snapshots."""
    before, first, second = captures
    if (not before[0] < first[0] < second[0]
            or any(end is not True for _, _, end in captures)):
        raise ValueError("ordered catalogs with LOG_REQUEST_END required")
    candidates = [i for i, size in first[1].items()
                  if before[1].get(i, 0) <= size < second[1].get(i, 0)]
    if len(candidates) != 1:
        raise ValueError("log growth cannot be attributed to one ID")
    return candidates[0]


def core_source(records, window_us, index):
    """Use only XKFS records inside an independently anchored boot window."""
    low, high = window_us
    if low >= high or type(index) is not int or index not in (0, 1, 2):
        raise ValueError("invalid window or source")
    cores = {0: [], 1: []}
    for row in records:
        if (row.get("mavpackettype") == "XKFS" and low <= row["TimeUS"] <= high
                and row["C"] in cores):
            cores[row["C"]].append(row["SS"])
    if any(not values or any(v != index for v in values) for values in cores.values()):
        raise ValueError("no consistent source evidence on both cores")
    return {core: len(values) for core, values in cores.items()}
