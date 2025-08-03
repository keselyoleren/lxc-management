
## Linux Container Management with LXC

This project is designed to help manage Linux containers (LXC) programmatically. LXC (Linux Containers) is a lightweight virtualization technology that allows you to run multiple isolated Linux systems (containers) on a single host. The worker service in this project can be extended to automate the creation, management, and monitoring of LXC containers, making it suitable for scenarios such as multi-tenant environments, sandboxing, or automated testing.

### How LXC is Used in This Project
- The `worker` service is configured with privileged access and mounts `/sys/fs/cgroup` and a persistent volume (`lxc_data`) to `/var/lib/lxc` to enable LXC operations inside the container.
- You can implement Celery tasks in `worker.py` to create, start, stop, or destroy LXC containers using Python libraries or shell commands.
- The architecture allows you to trigger container management tasks from the FastAPI web interface, which are then processed asynchronously by the worker.

> **Note:** To use LXC inside Docker, the worker container must run in privileged mode and have access to the host's cgroups and LXC storage, as configured in `docker-compose.yml`.

## Features
- **FastAPI** web server for handling HTTP requests
- **Celery** worker for background task processing
- **Redis** as a message broker for Celery
- Dockerized setup for easy deployment and development

## Project Structure
```
.
├── docker-compose.yml
├── Dockerfile
├── main.py           # FastAPI application
├── requirements.txt  # Python dependencies
├── settings.py       # Configuration
├── worker.py         # Celery app and tasks
├── templates/        # HTML templates
│   ├── base.html
│   └── index.html
└── __pycache__/
```

## Getting Started

### Prerequisites
- [Docker](https://www.docker.com/get-started)
- [Docker Compose](https://docs.docker.com/compose/)

### Quick Start
1. **Clone the repository:**
   ```sh
   git clone <your-repo-url>
   cd lxc-learn
   ```
2. **Build and start the services:**
   ```sh
   docker-compose up --build
   ```
3. **Access the web app:**
   - Open your browser and go to [http://localhost:8000](http://localhost:8000)

### Services
- **web**: FastAPI server (port 8000)
- **worker**: Celery worker for background tasks
- **redis**: Redis message broker (port 6379)

### Stopping the Project
To stop all services:
```sh
docker-compose down
```

## Configuration
- Environment variables and settings can be adjusted in `settings.py`.
- Python dependencies are listed in `requirements.txt`.

## License
MIT License
