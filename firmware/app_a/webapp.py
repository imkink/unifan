"""Microdot dashboard routes and small JSON payload builders."""

STATIC_DIR = __file__.rsplit("/", 1)[0] + "/static"


def _device_id():
    try:
        import machine
        try:
            import ubinascii as binascii
        except ImportError:
            import binascii
        return binascii.hexlify(machine.unique_id()).decode().upper()
    except (ImportError, AttributeError, OSError):
        return "UNKNOWN"


def build_status_payload(telemetry, ethernet, version, device_id):
    """Return the stable, browser-facing subset of the in-memory services."""
    snapshot = telemetry.snapshot()
    network = ethernet.status()
    ipv4 = network.get("ipv4")
    addresses = (list(ipv4) if isinstance(ipv4, (tuple, list)) and len(ipv4) == 4
                 else [None, None, None, None])
    return {
        "schema": 1,
        "environment": snapshot.get("environment"),
        "fans": snapshot.get("fans", []),
        "network": {
            "interface": network.get("interface"),
            "phase": network.get("phase"),
            "ready": bool(network.get("ready")),
            "ip": addresses[0],
            "netmask": addresses[1],
            "gateway": addresses[2],
            "dns": addresses[3],
            "error": network.get("error"),
        },
        "system": {
            "device_id": device_id,
            "version": version,
            "source": snapshot.get("source", "uninitialized"),
            "simulated": bool(snapshot.get("simulated", False)),
            "hardware_ready": bool(snapshot.get("hardware_ready", False)),
            "control_policy_ready": bool(snapshot.get("control_policy_ready", False)),
            "stale": bool(snapshot.get("stale", True)),
            "sequence": snapshot.get("sequence", 0),
        },
    }


def _read_asset(static_dir, name):
    with open(static_dir + "/" + name, "rb") as source:
        return source.read()


def register_routes(app, telemetry, ethernet, boot, static_dir=STATIC_DIR,
                    device_id=None):
    """Register the read-only dashboard; control and OTA remain separate."""
    if device_id is None:
        device_id = _device_id()

    asset_types = {
        "app.css": "text/css; charset=utf-8",
        "app.js": "application/javascript; charset=utf-8",
        "noun_Fan_8413191.svg": "image/svg+xml",
        "Rajdhani-Bold.woff2": "font/woff2",
        "Rajdhani-SemiBold.woff2": "font/woff2",
    }

    @app.get("/")
    async def index(request):
        return (_read_asset(static_dir, "index.html"), 200,
                {"Content-Type": "text/html; charset=utf-8",
                 "Cache-Control": "no-store"})

    @app.get("/api/status")
    async def status(request):
        return (build_status_payload(telemetry, ethernet, boot.version, device_id),
                200, {"Cache-Control": "no-store"})

    # Keep the catch-all route last so API paths always win on constrained routers.
    @app.get("/<path:path>")
    async def asset(request, path):
        content_type = asset_types.get(path)
        if content_type is None:
            return {"error": "Not found"}, 404
        try:
            body = _read_asset(static_dir, path)
        except OSError:
            return {"error": "Not found"}, 404
        return (body, 200, {"Content-Type": content_type,
                            # Asset names are stable across A/B upgrades; force revalidation.
                            "Cache-Control": "no-cache"})

    return {"device_id": device_id}
