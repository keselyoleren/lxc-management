
## Linux Container Management with LXC

This project is designed to help manage Linux containers (LXC) programmatically. LXC (Linux Containers) is a lightweight virtualization technology that allows you to run multiple isolated Linux systems (containers) on a single host. The worker service in this project can be extended to automate the creation, management, and monitoring of LXC containers, making it suitable for scenarios such as multi-tenant environments, sandboxing, or automated testing.

> **Important:** It is **not recommended** to run this project inside Docker, because LXC itself manages containers at the system level and may conflict with Docker's own containerization. For best results, run this project directly on a Linux host with LXC installed.

### How LXC is Used in This Project
- The `worker` service is designed to interact with LXC, managing containers programmatically.
- You can implement Celery tasks in `worker.py` to create, start, stop, or destroy LXC containers using Python libraries or shell commands.
- The architecture allows you to trigger container management tasks from the FastAPI web interface, which are then processed asynchronously by the worker.

## Features
- **FastAPI** web server for handling HTTP requests
- **Celery** worker for background task processing
- **Redis** as a message broker for Celery

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
- [LXC](https://linuxcontainers.org/lxc/introduction/) installed on your Linux host
- [Python 3.8+](https://www.python.org/downloads/)
- [Redis](https://redis.io/) server running

### Quick Start
1. **Clone the repository:**
    ```sh
    git clone <your-repo-url>
    cd lxc-learn
    ```
2. **Install dependencies:**
    ```sh
    pip install -r requirements.txt
    ```
3. **Start the services:**
    - Start Redis server if not already running.
    - Run the FastAPI server:
      ```sh
      uvicorn main:app --reload
      ```
    - Start the Celery worker:
      ```sh
      celery -A worker.celery worker --loglevel=info
      ```
4. **Access the web app:**
    - Open your browser and go to [http://localhost:8000](http://localhost:8000)

### Services
- **web**: FastAPI server (port 8000)
- **worker**: Celery worker for background tasks
- **redis**: Redis message broker (port 6379)

### Stopping the Project
To stop all services, simply terminate the running processes.

## Configuration
- Environment variables and settings can be adjusted in `settings.py`.
- Python dependencies are listed in `requirements.txt`.

## License
MIT License

