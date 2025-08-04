import lxc
import os
import pty
import select
import uuid
from threading import Thread
import asyncio
import json
import redis as sync_redis
import psutil  

from settings import REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND, PUBSUB_CHANNEL
from celery import Celery

celery_app = Celery(
    __name__,
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND
)

@celery_app.task
def get_host_stats_task():
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
    containers = []
    try:
        for c in lxc.list_containers(as_object=True):
            containers.append({"name": c.name, "state": c.state})
    except Exception as e:
        print(f"Error getting container list: {e}")
    return containers

@celery_app.task
def create_lxc_container_task(name, template, ram_limit, cpu_limit):
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_global_event(event_type, data):
        payload = json.dumps({"type": event_type, "data": data})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)

    try:
        c = lxc.Container(name)
        if c.defined:
            publish_global_event("notification", {"status": "error", "message": f"Container '{name}' already exists."})
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
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_global_event(event_type, data):
        payload = json.dumps({"type": event_type, "data": data})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)

    try:
        c = lxc.Container(name)
        if not c.defined:
            publish_global_event("notification", {"status": "error", "message": f"Container '{name}' not found."})
            return

        publish_global_event("notification", {"status": "info", "message": f"Starting action '{action}' on '{name}'..."})
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
        
        publish_global_event("list_update", {"message": f"Action '{action}' on container '{name}' succeeded."})

    except Exception as e:
        publish_global_event("notification", {"status": "error", "message": f"Action '{action}' on '{name}' failed: {e}"})


@celery_app.task
def get_container_stats_task(name: str):
    try:
        c = lxc.Container(name)
        if not c.running:
            return {"cpu_ns": 0, "ram_usage": 0, "ram_limit": 0, "disk_usage": 0, "disk_total": 0}
        
        cpu_usage_ns = int(c.get_cgroup_item("cpu.stat").splitlines()[1].split()[1])
        ram_usage_bytes = int(c.get_cgroup_item("memory.current"))
        ram_limit_bytes_str = c.get_cgroup_item("memory.max")
        ram_limit_bytes = int(ram_limit_bytes_str) if ram_limit_bytes_str.isdigit() else 0
        
        disk = psutil.disk_usage('/')
        disk_usage = disk.used
        disk_total = disk.total

        return {
            "cpu_ns": cpu_usage_ns,
            "ram_usage": ram_usage_bytes,
            "ram_limit": ram_limit_bytes,
            "disk_usage": disk_usage,
            "disk_total": disk_total
        }
    except (FileNotFoundError, IndexError):
        return {"cpu_ns": 0, "ram_usage": 0, "ram_limit": 0, "disk_usage": 0, "disk_total": 0}
    except Exception as e:
        print(f"Error getting stats for {name}: {e}")
        return None


@celery_app.task
def console_session_task(session_id, container_name):
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
            pubsub = raw_redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(input_ch)
            for msg in pubsub.listen():
                if msg['data'] == b'__TERMINATE__': break
                try:
                    os.write(fd, msg['data'])
                except OSError: break
        
        out_thread = Thread(target=pty_to_redis, daemon=True)
        in_thread = Thread(target=redis_to_pty, daemon=True)
        out_thread.start()
        in_thread.start()
        try: os.waitpid(pid, 0)
        finally:
            raw_redis_client.publish(input_ch, b'__TERMINATE__')
            out_thread.join(1); in_thread.join(1); os.close(fd); raw_redis_client.close()