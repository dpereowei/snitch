def restart_event_id(pod_uid, container, restart_count):
    return f"restart:{pod_uid}:{container}:{restart_count}"


def eviction_event_id(pod_uid):
    return f"eviction:{pod_uid}"