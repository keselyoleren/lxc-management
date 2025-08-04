import redis.asyncio as redis
import asyncio
import json
import uuid


from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from settings import *
from worker import console_session_task, container_action_task, create_lxc_container_task, get_all_containers_task, get_container_stats_task, get_host_stats_task

app = FastAPI(title="LXC Manager")
templates = Jinja2Templates(directory="templates")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/")
async def serve_spa(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/containers")
async def api_get_containers():
    task = get_all_containers_task.delay()
    containers = task.get(timeout=10)
    return JSONResponse(content=containers)

@app.post("/api/containers")
async def api_create_container(
    name: str = Form(...), template: str = Form("ubuntu"),
    ram_limit: str = Form(None), cpu_limit: str = Form(None)
):
    if not name:
        return JSONResponse(status_code=400, content={"message": "Container name is required."})
    
    create_lxc_container_task.delay(name, template, ram_limit, cpu_limit)
    return JSONResponse(content={"message": f"Creation task for '{name}' has been queued."})

@app.post("/api/containers/{name}/{action}")
async def api_container_action(name: str, action: str):
    if action not in ["start", "stop", "destroy"]:
        return JSONResponse(status_code=400, content={"message": "Aksi tidak valid."})
    container_action_task.delay(name, action)
    return JSONResponse(content={"message": f"Aksi '{action}' untuk '{name}' telah diantrekan."})


@app.websocket("/ws/events")
async def ws_global_events(websocket: WebSocket):
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
        while True: await websocket.receive_text() 
    except WebSocketDisconnect:
        print("Global event client disconnected.")
    finally:
        stop_event.set()
        await asyncio.gather(listener_task, sender_task, return_exceptions=True)


@app.websocket("/ws/container/{name}/stats")
async def ws_container_stats(websocket: WebSocket, name: str):
    await websocket.accept(); last_cpu_ns = 0; last_time = asyncio.get_event_loop().time()
    try:
        while True:
            stats = None
            try:
                task = get_container_stats_task.delay(name)
                stats = task.get(timeout=5)
            except Exception as e:
                print(f"Celery task for stats failed for {name}: {e}")
                await asyncio.sleep(2)
                continue

            if stats:
                current_time = asyncio.get_event_loop().time(); time_delta = current_time - last_time
                cpu_delta_ns = stats.get('cpu_ns', 0) - last_cpu_ns; cpu_percent = (cpu_delta_ns / 1_000_000_000) / time_delta * 100 if time_delta > 0 else 0
                last_cpu_ns = stats.get('cpu_ns', 0); last_time = current_time
                await websocket.send_json({"cpu": min(max(cpu_percent, 0), 100), "ram": stats.get('ram_usage', 0), "ram_limit": stats.get('ram_limit', 0), "disk_usage": stats.get('disk_usage', 0), "disk_total": stats.get('disk_total', 0)})
            else: 
                await websocket.send_json({"cpu": 0, "ram": 0, "ram_limit": 0, "disk_usage": 0, "disk_total": 0})
            await asyncio.sleep(2)
    except WebSocketDisconnect: 
        print(f"Stats client for {name} disconnected.")
    except Exception as e: 
        print(f"Unhandled error in stats websocket for {name}: {e}")

@app.websocket("/ws/console/{container_name}")
async def ws_console(websocket: WebSocket, container_name: str):
    await websocket.accept(); session_id = str(uuid.uuid4()); input_ch, output_ch = f"console_in:{session_id}", f"console_out:{session_id}"
    console_session_task.delay(session_id, container_name); stop_event = asyncio.Event()
    
    async def forward_to_redis(ws: WebSocket, stop_event: asyncio.Event):
        raw_publisher = redis.from_url(REDIS_URL)
        try:
            while not stop_event.is_set():
                data = await ws.receive_bytes()
                await raw_publisher.publish(input_ch, data)
        except WebSocketDisconnect: pass
        finally: 
            stop_event.set()
            await raw_publisher.publish(input_ch, b'__TERMINATE__')
            await raw_publisher.close()

    async def listen_from_redis(ws: WebSocket, stop_event: asyncio.Event):
        raw_listener = redis.from_url(REDIS_URL)
        pubsub = raw_listener.pubsub()
        await pubsub.subscribe(output_ch)
        try:
            while not stop_event.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg: await ws.send_bytes(msg['data'])
                await asyncio.sleep(0.01)
        finally: 
            await pubsub.unsubscribe(output_ch)
            await raw_listener.close()

    listener_task = asyncio.create_task(listen_from_redis(websocket, stop_event))
    forwarder_task = asyncio.create_task(forward_to_redis(websocket, stop_event))
    
    try: 
        await asyncio.gather(listener_task, forwarder_task)
    except Exception as e:
        print(f"Console websocket gather error: {e}")
    finally:
        stop_event.set()
        print(f"Console session for {container_name} closed.")
