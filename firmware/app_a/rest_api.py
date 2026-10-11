"""REST v1：鉴权与协议适配，不直接访问 GPIO，不因 GET 触发采样或写 Flash。"""
import gc
from mqtt_config import RevisionError


def register_routes(app, state, mqtt, settings, auth, https):
    request_sequence = [0]

    def response(request, data=None, status=200, code=None, headers=None):
        meta = state.meta()
        meta["request_id"] = getattr(request.g, "request_id", None)
        value = {"schema": 1, "meta": meta}
        value["error" if code else "data"] = {"code": code, "message": code, "field": None} if code else data
        output_headers = {"Cache-Control": "no-store"}
        if headers:
            output_headers.update(headers)
        return value, status, output_headers

    @app.before_request
    async def guard(request):
        request_sequence[0] += 1
        request.g.request_id = str(request_sequence[0])
        write = request.method not in ("GET", "HEAD", "OPTIONS")
        if not request.path.startswith("/api/"):
            return None
        if not auth.rate(False):
            return response(request, status=429, code="rate_limited", headers={"Retry-After": "1"})
        # 仅公开不含凭据的运行状态；诊断和配置必须 read/admin，写入必须 admin。
        public = request.path in ("/api/status", "/api/v1/info", "/api/v1/status", "/api/v1/health")
        if write or not public:
            denied = auth.authorize(request, write)
            if denied:
                return response(request, status=denied[0], code=denied[1])
        if https.maintenance:
            return response(request, status=503, code="maintenance", headers={"Retry-After": "5"})
        if write:
            if not auth.rate(True):
                return response(request, status=429, code="rate_limited", headers={"Retry-After": "1"})
            if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return response(request, status=415, code="json_required")

    @app.get("/api/v1/info")
    async def info(request):
        return response(request, state.info())

    @app.get("/api/v1/status")
    async def status(request):
        value = state.state()
        value["mqtt"] = mqtt.status()
        return response(request, value)

    @app.get("/api/v1/health")
    async def health(request):
        healthy = state.telemetry.healthy()
        return response(request, {"available": healthy, "source": state.telemetry.snapshot().get("source"),
                                  "hardware_ready": False}, 200 if healthy else 503)

    @app.get("/api/v1/diagnostics")
    async def diagnostics(request):
        value = {"python_heap_free": gc.mem_free(), "mqtt": mqtt.status(), "https": https.status()}
        try:
            import esp32
            rows = esp32.idf_heap_info(esp32.HEAP_DATA)
            value["native_free"] = sum(row[1] for row in rows)
            value["native_largest"] = max(row[2] for row in rows)
        except ImportError:
            pass
        return response(request, value)

    @app.put("/api/v1/system/time")
    async def set_time(request):
        try:
            value = request.json
        except (ValueError, TypeError):
            return response(request, status=400, code="invalid_json")
        if not isinstance(value, dict) or set(value) != {"unix_seconds"}:
            return response(request, status=422, code="unix_seconds_required")
        try:
            state.set_time(value["unix_seconds"])
        except ValueError:
            return response(request, status=422, code="invalid_unix_seconds")
        return response(request, {"time_synced": True, "utc": state.utc(), "source": "authenticated_admin"})

    @app.get("/api/v1/settings/mqtt")
    async def get_mqtt(request):
        return response(request, settings.public(), headers={"ETag": settings.etag()})

    @app.patch("/api/v1/settings/mqtt")
    async def patch_mqtt(request):
        expected = request.headers.get("If-Match")
        if expected is None:
            return response(request, status=428, code="if_match_required")
        try:
            changes = request.json
        except (ValueError, TypeError):
            return response(request, status=400, code="invalid_json")
        try:
            changed = settings.patch(changes, expected)
        except RevisionError:
            return response(request, status=412, code="revision_conflict")
        except ValueError as exc:
            return response(request, status=422, code=str(exc))
        except OSError:
            return response(request, status=507, code="storage_failed")
        value = settings.public()
        value["apply_status"] = "pending" if changed or mqtt.applied_revision != settings.revision else "applied"
        return response(request, value, headers={"ETag": settings.etag()})

    @app.errorhandler(404)
    async def not_found(request):
        return response(request, status=404, code="not_found")

    @app.errorhandler(405)
    async def not_allowed(request):
        return response(request, status=405, code="method_not_allowed")

    @app.errorhandler(500)
    async def internal(request):
        return response(request, status=500, code="internal_error")
