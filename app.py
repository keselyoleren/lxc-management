import lxc
import redis.asyncio as redis
from celery import Celery
from celery.result import AsyncResult
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
import asyncio
import json
import psutil
import pty
import os
import select
from threading import Thread
import uuid

# --- Konfigurasi & Inisialisasi ---
REDIS_URL = "redis://redis:6379/0"
PUBSUB_CHANNEL = "lxc_global_events"
app = FastAPI(title="LXC Manager")
templates = Jinja2Templates(directory="templates")
celery_app = Celery('tasks', broker=REDIS_URL, backend=REDIS_URL, include=['main'])
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# --- Celery Tasks ---

@celery_app.task
def get_host_stats_task():
    """Mengambil statistik CPU, RAM, dan Disk dari host."""
    cpu_usage = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        "cpu": cpu_usage,
        "ram": {"percent": ram.percent, "used": ram.used, "total": ram.total},
        "disk": {"percent": disk.percent, "used": disk.used, "total": disk.total}
    }

@celery_app.task
def get_all_containers_task():
    """Mengambil daftar ringkas semua container untuk sidebar."""
    containers = []
    try:
        for c in lxc.list_containers(as_object=True):
            containers.append({"name": c.name, "state": c.state})
    except Exception as e:
        print(f"Error getting container list: {e}")
    return containers

@celery_app.task
def create_lxc_container_task(name, template, ram_limit, cpu_limit):
    """Membuat container dan mempublikasikan event saat selesai."""
    import redis as sync_redis
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_global_event(event_type, data):
        payload = json.dumps({"type": event_type, "data": data})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)

    try:
        c = lxc.Container(name)
        if c.defined:
            publish_global_event("notification", {"status": "error", "message": f"Container '{name}' sudah ada."})
            return

        c.create(template)
        
        if ram_limit: c.set_config_item("lxc.cgroup2.memory.max", f"{ram_limit}M")
        if cpu_limit:
            try:
                num_cores = int(cpu_limit)
                if num_cores > 0: c.set_config_item("lxc.cgroup2.cpuset.cpus", f"0-{num_cores - 1}" if num_cores > 1 else "0")
            except (ValueError, TypeError): pass
        c.save_config()
        
        publish_global_event("list_update", {"message": f"Container '{name}' berhasil dibuat."})

    except Exception as e:
        publish_global_event("notification", {"status": "error", "message": f"Gagal membuat '{name}': {e}"})

@celery_app.task
def container_action_task(name, action):
    """Menjalankan aksi (start, stop, destroy) pada container."""
    import redis as sync_redis
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_global_event(event_type, data):
        payload = json.dumps({"type": event_type, "data": data})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)

    try:
        c = lxc.Container(name)
        if not c.defined:
            publish_global_event("notification", {"status": "error", "message": f"Container '{name}' tidak ditemukan."})
            return

        publish_global_event("notification", {"status": "info", "message": f"Memulai aksi '{action}' pada '{name}'..."})
        if action == "start":
            c.start()
            c.wait("RUNNING", 10)
        elif action == "stop":
            c.stop()
            c.wait("STOPPED", 10)
        elif action == "destroy":
            if c.running:
                c.stop()
                c.wait("STOPPED", 10)
            c.destroy()
        
        publish_global_event("list_update", {"message": f"Aksi '{action}' pada container '{name}' berhasil."})

    except Exception as e:
        publish_global_event("notification", {"status": "error", "message": f"Aksi '{action}' pada '{name}' gagal: {e}"})


@celery_app.task
def get_container_stats_task(name: str):
    """Mengambil statistik CPU & RAM untuk satu container."""
    try:
        c = lxc.Container(name)
        if not c.running:
            return {"cpu": 0, "ram_usage": 0, "ram_limit": 0}
        cpu_usage_ns = int(c.get_cgroup_item("cpu.stat").splitlines()[1].split()[1])
        ram_usage_bytes = int(c.get_cgroup_item("memory.current"))
        ram_limit_bytes_str = c.get_cgroup_item("memory.max")
        ram_limit_bytes = int(ram_limit_bytes_str) if ram_limit_bytes_str.isdigit() else 0
        return {"cpu_ns": cpu_usage_ns, "ram_usage": ram_usage_bytes, "ram_limit": ram_limit_bytes}
    except (FileNotFoundError, IndexError):
        return {"cpu": 0, "ram_usage": 0, "ram_limit": 0}
    except Exception as e:
        print(f"Error getting stats for {name}: {e}")
        return None

@celery_app.task
def console_session_task(session_id, container_name):
    """Menjalankan sesi PTY lxc-attach."""
    import redis as sync_redis
    raw_redis_client = sync_redis.from_url(REDIS_URL)
    input_ch, output_ch = f"console_in:{session_id}", f"console_out:{session_id}"
    
    pid, fd = pty.fork()
    if pid == 0:
        try: os.execvp("lxc-attach", ["lxc-attach", "-n", container_name])
        except Exception: os._exit(1)
    else:
        def pty_to_redis():
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if r:
                        data = os.read(fd, 1024)
                        if not data: break
                        raw_redis_client.publish(output_ch, data)
            finally:
                raw_redis_client.publish(output_ch, b'\r\n--- Console session ended ---\r\n')
        
        def redis_to_pty():
            pubsub = raw_redis_client.pubsub()
            pubsub.subscribe(input_ch)
            for msg in pubsub.listen():
                if msg['type'] == 'message':
                    if msg['data'] == b'__TERMINATE__': break
                    try: os.write(fd, msg['data'])
                    except OSError: break
        
        out_thread = Thread(target=pty_to_redis, daemon=True)
        in_thread = Thread(target=redis_to_pty, daemon=True)
        out_thread.start()
        in_thread.start()
        try: os.waitpid(pid, 0)
        finally:
            raw_redis_client.publish(input_ch, b'__TERMINATE__')
            out_thread.join(1); in_thread.join(1); os.close(fd); raw_redis_client.close()

# --- FastAPI Endpoints ---

@app.get("/")
async def serve_spa(request: Request):
    """Menyajikan kerangka utama aplikasi (Single Page Application)."""
    return templates.TemplateResponse("layout.html", {"request": request})

@app.get("/api/containers")
async def api_get_containers():
    """API untuk mendapatkan daftar semua container."""
    task = get_all_containers_task.delay()
    containers = task.get(timeout=10)
    return JSONResponse(content=containers)

@app.post("/api/containers")
async def api_create_container(
    name: str = Form(...), template: str = Form("ubuntu"),
    ram_limit: str = Form(None), cpu_limit: str = Form(None)
):
    """API untuk memicu pembuatan container baru."""
    if not name:
        return JSONResponse(status_code=400, content={"message": "Container name is required."})
    
    create_lxc_container_task.delay(name, template, ram_limit, cpu_limit)
    
    return JSONResponse(content={"message": f"Creation task for '{name}' has been queued."})

@app.post("/api/containers/{name}/{action}")
async def api_container_action(name: str, action: str):
    """API untuk memicu aksi pada container (start, stop, destroy)."""
    if action not in ["start", "stop", "destroy"]:
        return JSONResponse(status_code=400, content={"message": "Aksi tidak valid."})
    
    container_action_task.delay(name, action)
    
    return JSONResponse(content={"message": f"Aksi '{action}' untuk '{name}' telah diantrekan."})


# --- WebSocket Endpoints ---

@app.websocket("/ws/events")
async def ws_global_events(websocket: WebSocket):
    """WebSocket untuk event global dan statistik host."""
    await websocket.accept()
    
    async def redis_listener(ws: WebSocket, stop_event: asyncio.Event):
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(PUBSUB_CHANNEL)
        try:
            while not stop_event.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message: await ws.send_text(message['data'])
                await asyncio.sleep(0.01)
        finally: await pubsub.unsubscribe(PUBSUB_CHANNEL)

    async def host_stats_sender(ws: WebSocket, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                task = get_host_stats_task.delay()
                stats = task.get(timeout=5)
                payload = json.dumps({"type": "host_stats", "data": stats})
                await ws.send_text(payload)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Error sending host stats: {e}")

    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(redis_listener(websocket, stop_event))
    sender_task = asyncio.create_task(host_stats_sender(websocket, stop_event))
    
    try:
        while True: await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        print("Global event client disconnected.")
    finally:
        stop_event.set()
        await asyncio.gather(listener_task, sender_task, return_exceptions=True)


@app.websocket("/ws/container/{name}/stats")
async def ws_container_stats(websocket: WebSocket, name: str):
    """WebSocket untuk streaming statistik per container."""
    await websocket.accept()
    last_cpu_ns = 0
    last_time = asyncio.get_event_loop().time()
    try:
        while True:
            task = get_container_stats_task.delay(name)
            stats = task.get(timeout=5)
            
            if stats:
                current_time = asyncio.get_event_loop().time()
                time_delta = current_time - last_time
                cpu_delta_ns = stats['cpu_ns'] - last_cpu_ns
                cpu_percent = (cpu_delta_ns / 1_000_000_000) / time_delta * 100 if time_delta > 0 else 0
                last_cpu_ns = stats['cpu_ns']
                last_time = current_time
                
                await websocket.send_json({"cpu": min(max(cpu_percent, 0), 100), "ram": stats['ram_usage'], "ram_limit": stats['ram_limit']})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        print(f"Stats client for {name} disconnected.")
    except Exception as e:
        print(f"Error in stats websocket for {name}: {e}")

@app.websocket("/ws/console/{container_name}")
async def ws_console(websocket: WebSocket, container_name: str):
    """WebSocket untuk sesi terminal interaktif."""
    await websocket.accept()
    session_id = str(uuid.uuid4())
    input_ch, output_ch = f"console_in:{session_id}", f"console_out:{session_id}"
    
    console_session_task.delay(session_id, container_name)
    stop_event = asyncio.Event()
    
    async def forward_to_redis(ws: WebSocket):
        try:
            while not stop_event.is_set():
                data = await ws.receive_bytes()
                await redis_client.publish(input_ch, data)
        except WebSocketDisconnect: pass
        finally: stop_event.set(); await redis_client.publish(input_ch, b'__TERMINATE__')

    async def listen_from_redis(ws: WebSocket):
        raw_redis = redis.from_url(REDIS_URL)
        pubsub = raw_redis.pubsub()
        await pubsub.subscribe(output_ch)
        try:
            while not stop_event.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg: await ws.send_bytes(msg['data'])
                await asyncio.sleep(0.01)
        finally: await pubsub.unsubscribe(output_ch); await raw_redis.close()

    listener_task = asyncio.create_task(listen_from_redis(websocket))
    forwarder_task = asyncio.create_task(forward_to_redis(websocket))
    
    try: await asyncio.gather(listener_task, forwarder_task)
    except Exception: pass
    finally: stop_event.set(); print(f"Console session for {container_name} closed.")
