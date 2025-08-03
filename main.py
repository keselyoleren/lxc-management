import lxc
import redis.asyncio as redis
import uuid
import os
import asyncio
import json

from celery.result import AsyncResult
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates


from worker import console_session_task, container_action_task, create_lxc_container_task, get_host_stats_task, get_lxc_containers_info_task # Import library baru
from settings import REDIS_URL, PUBSUB_CHANNEL

app = FastAPI(title="LXC Manager")
templates = Jinja2Templates(directory="templates")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)



@app.get("/")
def read_root(request: Request):
    containers = []
    try:
        task_result: AsyncResult = get_lxc_containers_info_task.delay()
        containers = task_result.get(timeout=10)
    except Exception as e:
        print(f"Could not get container list from worker: {e}")
    
    return templates.TemplateResponse("index.html", {"request": request, "containers": containers})

@app.post("/create")
async def create_container_endpoint(
    name: str = Form(...), template: str = Form(...),
    ram_limit: str = Form(None), cpu_limit: str = Form(None)
):
    create_lxc_container_task.delay(name, template, ram_limit, cpu_limit)
    return RedirectResponse(url="/", status_code=303)

@app.post("/container/{name}/{action}")
async def container_action_endpoint(name: str, action: str):
    container_action_task.delay(name, action)
    return RedirectResponse(url="/", status_code=303, headers={"Cache-Control": "no-store"})

@app.get("/container/{container_name}/console")
async def console_page(request: Request, container_name: str):
    return templates.TemplateResponse("console.html", {"request": request, "container_name": container_name})


async def redis_listener(websocket: WebSocket, stop_event: asyncio.Event):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL)
    try:
        while not stop_event.is_set():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message:
                await websocket.send_text(message['data'])
            await asyncio.sleep(0.01)
    except Exception as e:
        print(f"Redis listener error: {e}")
    finally:
        await pubsub.unsubscribe(PUBSUB_CHANNEL)

async def host_stats_sender(websocket: WebSocket, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            task = get_host_stats_task.delay()
            stats = task.get(timeout=5)
            payload = json.dumps({"type": "host_stats", "data": stats})
            await websocket.send_text(payload)
            await asyncio.sleep(2) 
    except Exception as e:
        print(f"Host stats sender error: {e}")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(redis_listener(websocket, stop_event))
    sender_task = asyncio.create_task(host_stats_sender(websocket, stop_event))
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        print("Client disconnected.")
    finally:
        stop_event.set()
        await asyncio.gather(listener_task, sender_task, return_exceptions=True)

@app.websocket("/ws/console/{container_name}")
async def console_websocket(websocket: WebSocket, container_name: str):
    await websocket.accept()
    session_id = str(uuid.uuid4())
    input_channel = f"console_input:{session_id}"
    output_channel = f"console_output:{session_id}"
    
    console_session_task.delay(session_id, container_name)
    stop_event = asyncio.Event()
    
    async def forward_to_redis(ws: WebSocket):
        try:
            while not stop_event.is_set():
                ws_data = await ws.receive_text()
                msg = json.loads(ws_data)
                if msg.get('type') == 'stdin':
                    await redis_client.publish(input_channel, msg['data'].encode('utf-8'))
        except (WebSocketDisconnect, json.JSONDecodeError): pass
        finally: stop_event.set(); await redis_client.publish(input_channel, b'__TERMINATE__')

    async def listen_from_redis(ws: WebSocket):
        # Gunakan client redis terpisah untuk data byte mentah
        raw_redis_client = redis.from_url(REDIS_URL)
        pubsub = raw_redis_client.pubsub()
        await pubsub.subscribe(output_channel)
        try:
            while not stop_event.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message: await ws.send_bytes(message['data'])
                await asyncio.sleep(0.01)
        finally: await pubsub.unsubscribe(output_channel); await raw_redis_client.close()

    listener_task = asyncio.create_task(listen_from_redis(websocket))
    forwarder_task = asyncio.create_task(forward_to_redis(websocket))
    
    try:
        await asyncio.gather(listener_task, forwarder_task)
    except Exception as e: print(f"Console websocket error: {e}")
    finally: stop_event.set(); print(f"Console session for {container_name} closed.")
