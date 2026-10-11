"""Temporary MicroPython HTTPS benchmark; never installed as main.py.

Run only through https_probe.py. Uses the project simulation and Microdot,
with an explicit nonblocking TLS wrapper (never Microdot's plaintext fallback).
All HTTP routes require a per-run token. No settings/OTA writes are exposed.
"""
import asyncio
import gc
import json
import machine
import os
import ssl
import time
import esp32
from microdot import Microdot, Request
from ethernet import W5500Ethernet
from hardware import create_hardware
from telemetry import TelemetryService
from webapp import register_routes
import probe_config as cfg

stats = {}
active = 0
maintenance = False
stopping = False
ota_task = None
sample_task = None
telemetry = None
ethernet = None
started = time.ticks_ms()


def native_heap():
    rows = esp32.idf_heap_info(esp32.HEAP_DATA)
    return sum(row[1] for row in rows), max(row[2] for row in rows)


def reset_stats():
    gc.collect()
    free, largest = native_heap()
    stats.clear()
    stats.update({"heap_min": gc.mem_free(), "native_free_min": free,
                  "native_largest_min": largest, "lag_max_ms": 0,
                  "supervisor_gap_max_ms": 0, "active_max": active,
                  "requests": 0, "transport_errors": 0, "rejected": 0,
                  "samples_start": telemetry.snapshot().get("sequence", 0),
                  "ota": {"phase": "idle"}})


def metrics(collect=False):
    if collect:
        gc.collect()
    free, largest = native_heap()
    value = dict(stats)
    value.update({"heap_free": gc.mem_free(), "native_free": free,
                  "native_largest": largest, "active": active,
                  "uptime_ms": time.ticks_diff(time.ticks_ms(), started),
                  "sample_sequence": telemetry.snapshot().get("sequence", 0),
                  "sample_healthy": telemetry.healthy(),
                  "maintenance": maintenance})
    return value


async def monitor():
    count = 0
    previous = time.ticks_ms()
    while not stopping:
        await asyncio.sleep_ms(100)
        now = time.ticks_ms()
        stats["lag_max_ms"] = max(stats["lag_max_ms"],
                                  max(0, time.ticks_diff(now, previous) - 100))
        previous = now
        stats["heap_min"] = min(stats["heap_min"], gc.mem_free())
        count += 1
        if count % 10 == 0:
            free, largest = native_heap()
            stats["native_free_min"] = min(stats["native_free_min"], free)
            stats["native_largest_min"] = min(stats["native_largest_min"], largest)


async def supervision(wdt, network_task, http_server, https_server):
    previous = time.ticks_ms()
    while not stopping:
        now = time.ticks_ms()
        stats["supervisor_gap_max_ms"] = max(
            stats["supervisor_gap_max_ms"], time.ticks_diff(now, previous))
        previous = now
        if network_task.done():
            raise RuntimeError("Network monitor stopped")
        if not maintenance and (sample_task.done() or not telemetry.healthy()):
            raise RuntimeError("Sample task stopped or stale")
        if time.ticks_diff(now, started) > cfg.MAX_RUNTIME_SECONDS * 1000:
            raise RuntimeError("Probe maximum runtime reached")
        if maintenance and time.ticks_diff(now, stats["ota"]["started_ms"]) > 200000:
            raise RuntimeError("Maintenance deadline exceeded")
        wdt.feed()
        await asyncio.sleep(1)


async def close_stream(writer):
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1)
    except (OSError, ValueError, AttributeError, asyncio.TimeoutError):
        pass


async def download_fixture():
    """Exercise outgoing verified TLS + small Flash writes, NOT firmware OTA."""
    global maintenance, sample_task
    maintenance = True
    stats["ota"] = {"phase": "starting", "bytes": 0, "started_ms": time.ticks_ms()}
    sample_task.cancel()
    try:
        await sample_task
    except asyncio.CancelledError:
        pass
    gc.collect()
    writer = None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=cfg.ROOT + "/ca.pem")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            cfg.HOST_IP, cfg.HOST_PORT, ssl=context, server_hostname=cfg.HOST_IP), 10)
        writer.write(("GET /fixture HTTP/1.1\r\nHost: " + cfg.HOST_IP +
                      "\r\nConnection: close\r\n\r\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 10)
        if b" 200 " not in line:
            raise OSError("Fixture HTTP status not 200")
        while await asyncio.wait_for(reader.readline(), 10) != b"\r\n":
            pass
        stats["ota"]["phase"] = "downloading"
        with open(cfg.ROOT + "/download.bin", "wb") as target:
            while True:
                if time.ticks_diff(time.ticks_ms(), stats["ota"]["started_ms"]) > 180000:
                    raise asyncio.TimeoutError("Fixture overall deadline exceeded")
                data = await asyncio.wait_for(reader.read(2048), 10)
                if not data:
                    break
                if target.write(data) != len(data):
                    raise OSError("Short fixture write")
                stats["ota"]["bytes"] += len(data)
                if stats["ota"]["bytes"] > cfg.FIXTURE_BYTES:
                    raise ValueError("Fixture too large")
                await asyncio.sleep_ms(0)
        if stats["ota"]["bytes"] != cfg.FIXTURE_BYTES:
            raise ValueError("Fixture truncated")
        os.sync()
        stats["ota"]["phase"] = "done"
    except Exception as exc:
        stats["ota"]["phase"] = "error"
        stats["ota"]["error"] = repr(exc)[:160]
    finally:
        if writer is not None:
            await close_stream(writer)
        try:
            os.remove(cfg.ROOT + "/download.bin")
        except OSError:
            pass
        # Simulation only: no hardware output or production OTA state is touched.
        await telemetry.sample_once()
        sample_task = asyncio.create_task(telemetry.run())
        maintenance = False


async def main():
    global telemetry, ethernet, sample_task, stopping, ota_task, active
    gc.collect()
    print("PROBE_IMPORT_HEAP", gc.mem_free())
    ethernet = W5500Ethernet()
    ethernet.start()
    if not await ethernet.wait_ready():
        raise RuntimeError("W5500 not ready")
    sensor, fans = create_hardware()
    if not sensor.simulated or not fans.simulated:
        raise RuntimeError("Probe only supports explicit simulation")
    telemetry = TelemetryService(sensor, fans)
    await telemetry.initialize()
    reset_stats()
    app = Microdot()
    Request.max_content_length = 1024
    Request.max_body_length = 1024
    Request.max_readline = 2048

    @app.before_request
    async def authorize(request):
        if request.headers.get("Authorization") != "Bearer " + cfg.TOKEN:
            return {"error": "Unauthorized"}, 401
        if maintenance and not request.path.startswith("/__probe/"):
            return {"error": "maintenance"}, 503
        stats["requests"] += 1

    @app.get("/__probe/metrics")
    async def get_metrics(request):
        return metrics(request.args.get("gc") == "1")

    @app.post("/__probe/reset")
    async def reset(request):
        if maintenance:
            return {"error": "busy"}, 409
        reset_stats()
        return metrics()

    @app.post("/__probe/download")
    async def download(request):
        global ota_task
        if ota_task is not None and not ota_task.done():
            return {"error": "busy"}, 409
        ota_task = asyncio.create_task(download_fixture())
        return {"accepted": True}, 202

    @app.post("/__probe/stop")
    async def stop(request):
        global stopping
        if maintenance:
            return {"error": "busy"}, 409
        stopping = True
        return {"stopping": True}

    class Boot:
        version = "https-probe"
    register_routes(app, telemetry, ethernet, Boot(), cfg.ROOT + "/static")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cfg.ROOT + "/server.pem", cfg.ROOT + "/server.key")
    print("PROBE_CONTEXT_HEAP", gc.mem_free())

    async def serve(reader, writer, use_tls):
        global active
        if active >= cfg.MAX_CONNECTIONS:
            stats["rejected"] += 1
            await close_stream(writer)
            return
        active += 1
        stats["active_max"] = max(stats["active_max"], active)
        try:
            if use_tls:
                # MicroPython 1.29 Stream stores its pollable socket in .s.
                # Explicitly wrap before any HTTP parsing; failures close, never downgrade.
                raw = writer.s
                wrapped = context.wrap_socket(raw, server_side=True,
                                              do_handshake_on_connect=False)
                wrapped.setblocking(False)
                reader.s = wrapped
                writer.s = wrapped
            await asyncio.wait_for(app.handle_request(reader, writer), 8)
        except Exception as exc:
            stats["transport_errors"] += 1
            print("PROBE_TRANSPORT", repr(exc)[:120])
        finally:
            await close_stream(writer)
            active -= 1

    async def plain(reader, writer):
        await serve(reader, writer, False)

    async def secure(reader, writer):
        await serve(reader, writer, True)

    http_server = await asyncio.start_server(plain, "0.0.0.0", cfg.HTTP_PORT, backlog=4)
    https_server = await asyncio.start_server(secure, "0.0.0.0", cfg.HTTPS_PORT, backlog=4)
    network_task = asyncio.create_task(ethernet.run())
    sample_task = asyncio.create_task(telemetry.run())
    monitor_task = asyncio.create_task(monitor())
    wdt = machine.WDT(timeout=30000)
    supervisor_task = asyncio.create_task(supervision(wdt, network_task, http_server, https_server))
    gc.collect()
    print("PROBE_READY", json.dumps({"ip": ethernet.status()["ipv4"][0],
          "heap": gc.mem_free(), "cpu_hz": machine.freq(), "metrics": metrics()}))
    try:
        while not stopping:
            if supervisor_task.done():
                await supervisor_task
                raise RuntimeError("Supervisor exited")
            await asyncio.sleep_ms(100)
        await asyncio.sleep(1)
    finally:
        print("PROBE_FINAL", json.dumps(metrics(True)))
        http_server.close()
        https_server.close()
        for task in (network_task, sample_task, monitor_task, supervisor_task):
            task.cancel()


def run():
    asyncio.run(main())
