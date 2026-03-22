#!/usr/bin/env python3
"""EMO BLE Bridge — HTTP API to control EMO robot via Bluetooth LE.

Endpoints:
  GET  /health              — connection status
  GET  /status              — full EMO status
  GET  /connect             — force reconnect
  GET  /disconnect          — disconnect BLE
  GET  /dances              — available dance list
  POST /dance               — {"num": 0-10}
  POST /stop_dance          — exit animation mode
  POST /move                — {"direction": "forward|back|left|right|stop"}
  POST /animation           — {"name": "interact_emotion_happy"}
  POST /volume              — {"level": 0-5}
  POST /power_off           — shutdown EMO
  POST /photo               — take photo from EMO camera, returns JPEG
  POST /face                — {"op": "syn|add|del|rename", ...}
  POST /raw                 — {"cmd": "JSON string"} raw BLE command
"""

import asyncio
import io
import json
import os
import logging
import socket
import struct
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from bleak import BleakClient, BleakScanner, BLEDevice, AdvertisementData
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

logging.basicConfig(format="[BLE] %(asctime)s %(message)s", level=logging.INFO)
logger = logging.getLogger()

PORT = int(os.environ.get("PORT", "8091"))
PHOTO_PORT = int(os.environ.get("PHOTO_PORT", "8099"))
EMO_ADDR = os.environ.get("EMO_ADDR", "")
BLE_ADAPTER = os.environ.get("BLE_ADAPTER", "")
SERVER_IP = os.environ.get("SERVER_IP", "")

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

DANCE_LIST = [
    "d1_EmoDance", "d2_WontLetGo", "d3_Blindless", "d4_Click1",
    "d5_TimeOfMyLife", "d6_Rollercoaster", "d7_FlashBack", "d8_Click2",
    "d9_BlameYourself", "d10_CanITakeYouThere", "d11_OceanBlue",
]

SEQ = 1


def encode_text(payload: str) -> bytes:
    data = payload.encode("utf-8")
    return bytes([0xBB, 0xAA]) + len(data).to_bytes(2, "little") + data


def encode_cmd(data: list, sequential=True) -> bytes:
    global SEQ
    payload = bytes([0xDD, 0xCC, SEQ if sequential else 0]) + bytes(data) + bytes(17 - len(data))
    if sequential:
        SEQ = (SEQ % 254) + 1
    return payload


def detect_server_ip():
    """Detect our IP on the local network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.109"


class EmoConnection:
    def __init__(self):
        self.client = None
        self.char = None
        self.response = None
        self.response_event = asyncio.Event()
        self.connected = False
        self.loop = None
        self._buf = bytearray()
        self._expected = 0

    def _handle_rx(self, _sender, data: bytearray):
        if data[0] == 0xBB and data[1] == 0xAA:
            self._expected = int.from_bytes(data[2:4], "little")
            self._buf = bytearray(data[4:])
        elif data[0] == 0xDD and data[1] == 0xCC:
            self.response = {"_bin": data.hex()}
            self.response_event.set()
            return
        else:
            self._buf += data

        if len(self._buf) >= self._expected and self._expected > 0:
            try:
                self.response = json.loads(self._buf[: self._expected].decode("utf-8"))
            except Exception:
                self.response = {"_raw": self._buf[: self._expected].hex()}
            self.response_event.set()
            self._buf = bytearray()
            self._expected = 0

    async def connect(self):
        if self.connected and self.client and self.client.is_connected:
            return True

        logger.info("Scanning for EMO...")
        kwargs = {}
        if BLE_ADAPTER:
            kwargs["adapter"] = BLE_ADAPTER

        if EMO_ADDR:
            device = await BleakScanner.find_device_by_address(EMO_ADDR, timeout=15, **kwargs)
        else:
            def match(d: BLEDevice, adv: AdvertisementData):
                return SERVICE_UUID.lower() in adv.service_uuids
            device = await BleakScanner.find_device_by_filter(match, timeout=15, **kwargs)

        if not device:
            logger.error("EMO not found")
            return False

        logger.info(f"Found: {device.name} ({device.address})")
        connect_kwargs = {}
        if BLE_ADAPTER:
            connect_kwargs["adapter"] = BLE_ADAPTER
        self.client = await establish_connection(
            BleakClientWithServiceCache, device, "EMO", **connect_kwargs
        )
        svc = self.client.services.get_service(SERVICE_UUID)
        self.char = svc.get_characteristic(CHAR_UUID)
        await self.client.start_notify(self.char, self._handle_rx)
        self.connected = True
        logger.info("Connected to EMO")
        return True

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.connected = False

    async def _send_fragmented(self, payload: bytes):
        for i in range(0, len(payload), 20):
            await self.client.write_gatt_char(self.char, payload[i : i + 20], response=False)
            await asyncio.sleep(0.05)

    async def send_request(self, payload: bytes, timeout=5) -> dict:
        if not self.connected:
            await self.connect()
        self.response = None
        self.response_event.clear()
        await self._send_fragmented(payload)
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout)
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        return self.response or {"error": "no response"}

    async def send_command(self, payload: bytes):
        if not self.connected:
            await self.connect()
        await self.client.write_gatt_char(self.char, payload, response=False)

    # ── High-level API ────────────────────────────────────────────

    async def get_status(self):
        req = encode_text('{"data":{"request":[0,1,2,7,8,11,12,13,14]},"type":"sta_req"}')
        return await self.send_request(req)

    async def get_full_status(self):
        result = {}
        for i in range(15):
            req = encode_text(json.dumps({"data": {"request": [i]}, "type": "sta_req"}))
            r = await self.send_request(req, timeout=3)
            if r and "data" in r:
                result.update(r["data"])
            await asyncio.sleep(0.3)
        return {"type": "sta_rsp", "data": result}

    async def dance(self, num=0):
        if num < 0 or num >= len(DANCE_LIST):
            num = 0
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        name = DANCE_LIST[num]
        resp = await self.send_request(
            encode_text(json.dumps({"data": {"name": name, "op": "play"}, "type": "anim_req"}))
        )
        return resp

    async def stop_dance(self):
        return await self.send_request(encode_text('{"data":{"op":"out"},"type":"anim_req"}'))

    async def play_animation(self, name: str):
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        resp = await self.send_request(
            encode_text(json.dumps({"data": {"name": name, "op": "play"}, "type": "anim_req"}))
        )
        return resp

    async def move(self, direction: str):
        dirs = {"forward": 5, "back": 6, "left": 7, "right": 8, "stop": 9}
        code = dirs.get(direction, 9)
        await self.send_command(encode_cmd([3, 4, code], sequential=False))
        return {"ok": True, "direction": direction}

    async def power_off(self):
        return await self.send_request(encode_text('{"data":{},"type":"off_req"}'))

    async def set_volume(self, level: int):
        req = encode_text(json.dumps({"data": {"volume": level}, "type": "setting_req"}))
        return await self.send_request(req)

    async def face_op(self, data: dict):
        return await self.send_request(
            encode_text(json.dumps({"data": data, "type": "face_req"}))
        )

    async def take_photo(self) -> bytes:
        """Request photo from EMO camera via BLE+TCP. Returns JPEG bytes."""
        server_ip = SERVER_IP or detect_server_ip()
        photo_data = None
        photo_event = threading.Event()

        def tcp_receiver():
            nonlocal photo_data
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PHOTO_PORT))
            srv.listen(1)
            srv.settimeout(15)
            try:
                conn, addr = srv.accept()
                logger.info(f"Photo TCP from {addr}")
                # Read header until #
                header = b""
                while b"#" not in header:
                    chunk = conn.recv(1)
                    if not chunk:
                        break
                    header += chunk
                header_str = header.decode("utf-8", errors="replace").strip("#")
                parts = {}
                for p in header_str.split(";"):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        parts[k] = v
                filesize = int(parts.get("filesize", 0))
                logger.info(f"Photo: {parts.get('name')}, {filesize} bytes")
                # Read JPEG data
                data = b""
                while len(data) < filesize:
                    chunk = conn.recv(min(filesize - len(data), 8192))
                    if not chunk:
                        break
                    data += chunk
                # Read trailing delimiter
                conn.recv(1024)
                conn.close()
                photo_data = data
                logger.info(f"Photo received: {len(data)} bytes")
            except socket.timeout:
                logger.warning("Photo TCP timeout")
            except Exception as e:
                logger.error(f"Photo TCP error: {e}")
            finally:
                srv.close()
                photo_event.set()

        # Start TCP server in background
        tcp_thread = threading.Thread(target=tcp_receiver, daemon=True)
        tcp_thread.start()
        await asyncio.sleep(0.3)

        # Send BLE commands
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"photo_req"}'), timeout=3)
        await asyncio.sleep(0.5)
        cmd = json.dumps({
            "type": "photo_req",
            "data": {"op": "syn", "server": {"ip": server_ip, "port": PHOTO_PORT}},
        })
        await self.send_request(encode_text(cmd), timeout=10)

        # Wait for photo
        photo_event.wait(timeout=20)
        return photo_data


emo = EmoConnection()


def json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def binary_response(handler, status, data, content_type="image/jpeg"):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, emo.loop)
        return future.result(timeout=30)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                json_response(self, 200, {
                    "status": "ok",
                    "connected": emo.connected,
                    "emo_addr": EMO_ADDR,
                    "adapter": BLE_ADAPTER,
                })
            elif path == "/status":
                json_response(self, 200, self._run(emo.get_status()))
            elif path == "/status/full":
                json_response(self, 200, self._run(emo.get_full_status()))
            elif path == "/dances":
                json_response(self, 200, {"dances": DANCE_LIST})
            elif path == "/connect":
                json_response(self, 200, {"connected": self._run(emo.connect())})
            elif path == "/disconnect":
                self._run(emo.disconnect())
                json_response(self, 200, {"disconnected": True})
            else:
                json_response(self, 404, {"error": "not found"})
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}
        path = urlparse(self.path).path

        try:
            if path == "/dance":
                json_response(self, 200, self._run(emo.dance(body.get("num", 0))))
            elif path == "/stop_dance":
                json_response(self, 200, self._run(emo.stop_dance()))
            elif path == "/move":
                json_response(self, 200, self._run(emo.move(body.get("direction", "stop"))))
            elif path == "/animation":
                json_response(self, 200, self._run(emo.play_animation(body.get("name", ""))))
            elif path == "/volume":
                json_response(self, 200, self._run(emo.set_volume(body.get("level", 3))))
            elif path == "/power_off":
                json_response(self, 200, self._run(emo.power_off()))
            elif path == "/face":
                json_response(self, 200, self._run(emo.face_op(body)))
            elif path == "/photo":
                photo = self._run(emo.take_photo())
                if photo:
                    binary_response(self, 200, photo)
                else:
                    json_response(self, 504, {"error": "photo capture failed"})
            elif path == "/raw":
                cmd = body.get("cmd", "")
                json_response(self, 200, self._run(emo.send_request(encode_text(cmd))))
            else:
                json_response(self, 404, {"error": "not found"})
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def log_message(self, fmt, *args):
        logger.info(fmt % args)


def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    emo.loop = loop
    threading.Thread(target=run_async_loop, args=(loop,), daemon=True).start()

    # Auto-detect server IP
    if not SERVER_IP:
        SERVER_IP = detect_server_ip()
        logger.info(f"Server IP: {SERVER_IP}")

    # Auto-connect
    try:
        future = asyncio.run_coroutine_threadsafe(emo.connect(), loop)
        future.result(timeout=20)
    except Exception as e:
        logger.warning(f"Auto-connect failed: {e}")

    logger.info(f"EMO BLE Bridge on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
