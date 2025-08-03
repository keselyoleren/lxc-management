import lxc
import redis.asyncio as redis

import asyncio
import json

from celery.result import AsyncResult
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates


from worker import container_action_task, create_lxc_container_task, get_host_stats_task, get_lxc_containers_info_task # Import library baru
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
