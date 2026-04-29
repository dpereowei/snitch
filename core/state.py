class PodState:
    def __init__(self):
        self.data = {}  # pod_uid -> {container: restart_count}

    def get(self, pod_uid):
        return self.data.get(pod_uid, {})

    def update(self, pod_uid, container_map):
        self.data[pod_uid] = container_map