def deployment_zone(host):
    if not host:
        raise ValueError("host must be nonempty")
    raise RuntimeError("Deployment inventory is external and was not provided; no default zone exists")
