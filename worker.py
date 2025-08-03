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
def get_lxc_containers_info_task():
    containers_info = []
    try:
        for container in lxc.list_containers(as_object=True):
            ips = container.get_ips(family="inet")
            ram_limit = container.get_config_item("lxc.cgroup2.memory.max") or "Default"
            cpu_limit = container.get_config_item("lxc.cgroup2.cpuset.cpus") or "Default"
            containers_info.append({
                "name": container.name, "state": container.state, "ips": ips,
                "ram_limit": ram_limit, "cpu_limit": cpu_limit,
            })
    except Exception as e:
        print(f"Error getting container list from worker: {e}")
    return containers_info

@celery_app.task
def create_lxc_container_task(name, template, ram_limit, cpu_limit):
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_notification(message):
        payload = json.dumps({"type": "notification", "data": {"message": message}})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)

    try:
        publish_notification(f"Memulai proses pembuatan container '{name}'...")
        c = lxc.Container(name)
        if c.defined:
            publish_notification(f"Gagal: Container '{name}' sudah ada.")
            return

        c.create(template)
        
        if ram_limit: c.set_config_item("lxc.cgroup2.memory.max", f"{ram_limit}M")
        if cpu_limit:
            try:
                num_cores = int(cpu_limit)
                if num_cores > 0:
                    cpu_set = f"0-{num_cores - 1}" if num_cores > 1 else "0"
                    c.set_config_item("lxc.cgroup2.cpuset.cpus", cpu_set)
            except (ValueError, TypeError): pass

        c.save_config()
        publish_notification(f"Container '{name}' berhasil dibuat! Halaman akan dimuat ulang.")
    except Exception as e:
        publish_notification(f"Gagal membuat container '{name}': {e}")


@celery_app.task
def container_action_task(name, action):
    sync_redis_client = sync_redis.from_url(REDIS_URL, decode_responses=True)
    
    def publish_status(state, ips=None):
        if ips is None:
            ips = []
        payload = json.dumps({"type": "status_update", "data": {"name": name, "state": state, "ips": ips}})
        sync_redis_client.publish(PUBSUB_CHANNEL, payload)
        
    try:
        c = lxc.Container(name)
        if not c.defined:
            return
        
        if action == "start":
            publish_status("STARTING")
            c.start()
            c.wait("RUNNING", 10)
            publish_status("RUNNING", c.get_ips(family="inet"))
        elif action == "stop":
            publish_status("STOPPING")
            c.stop()
            c.wait("STOPPED", 10)
            publish_status("STOPPED")
        elif action == "destroy":
            publish_status("DELETING")
            if c.running:
                c.stop()
                c.wait("STOPPED", 10)
            c.destroy()
            publish_status("DELETED")
    except Exception as e:
        publish_status(c.state, c.get_ips(family="inet")) 
        print(f"Error during action '{action}' on '{name}': {e}")

@celery_app.task
def console_session_task(session_id, container_name):
    raw_redis_client = sync_redis.from_url(REDIS_URL)
    
    input_channel = f"console_input:{session_id}"
    output_channel = f"console_output:{session_id}"
    
    pid, fd = pty.fork()
    
    if pid == 0: # Proses anak
        try:
            os.execvp("lxc-attach", ["lxc-attach", "-n", container_name])
        except Exception as e:
            print(f"Error executing lxc-attach: {e}")
            os._exit(1)
    else: # Proses induk
        def pty_to_redis():
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if r:
                        data = os.read(fd, 1024)
                        if not data: break
                        raw_redis_client.publish(output_channel, data)
            finally:
                raw_redis_client.publish(output_channel, b'\r\n--- Console session ended ---\r\n')

        def redis_to_pty():
            pubsub = raw_redis_client.pubsub()
            pubsub.subscribe(input_channel)
            for message in pubsub.listen():
                if message['type'] == 'message':
                    data = message['data']
                    if data == b'__TERMINATE__': break
                    try:
                        os.write(fd, data)
                    except OSError:
                        break

        output_thread = Thread(target=pty_to_redis, daemon=True)
        input_thread = Thread(target=redis_to_pty, daemon=True)
        output_thread.start()
        input_thread.start()
        
        try:
            os.waitpid(pid, 0)
        finally:
            raw_redis_client.publish(input_channel, b'__TERMINATE__')
            output_thread.join(timeout=1)
            input_thread.join(timeout=1)
            os.close(fd)
            raw_redis_client.close()
