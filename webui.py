from __future__ import annotations

import os
import time
import asyncio
from threading import Thread

from modules import timer
from modules import initialize_util
from modules import initialize

startup_timer = timer.startup_timer
startup_timer.record("launcher")

initialize.imports()
initialize.check_versions()


# ------------------- NEW FEATURES -------------------

def add_healthcheck(app):
    """Add /health and /metrics endpoints"""
    from fastapi import APIRouter
    import psutil

    router = APIRouter()

    @router.get("/health")
    def health():
        return {"status": "ok", "uptime": time.time() - startup_timer.start_time}

    @router.get("/metrics")
    def metrics():
        return {
            "cpu_percent": psutil.cpu_percent(),
            "memory": psutil.virtual_memory()._asdict(),
            "uptime": time.time() - startup_timer.start_time,
        }

    app.include_router(router)


def reload_config():
    """Reload configuration without restarting the server"""
    from modules import shared
    shared.opts.reload()
    print("✅ Configuration reloaded without full restart.")


def background_scheduler():
    """Run periodic background jobs"""
    import schedule
    from modules import ui_tempdir

    def job():
        print("🧹 Cleaning temporary files...")
        ui_tempdir.cleanup_tmpdr()

    schedule.every(1).hours.do(job)

    while True:
        schedule.run_pending()
        time.sleep(10)


def start_scheduler():
    t = Thread(target=background_scheduler, daemon=True)
    t.start()


def add_control_endpoints(app):
    """Expose REST API for server control"""
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/control/{action}")
    def control(action: str):
        from modules import shared
        if action in ("stop", "restart"):
            shared.state.set_server_command(action)
            return {"message": f"Server {action} triggered."}
        elif action == "reload_config":
            reload_config()
            return {"message": "Config reloaded."}
        return {"error": "Invalid action."}

    app.include_router(router)


def add_websocket(app):
    """Expose websocket for server notifications"""
    from fastapi import WebSocket

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        await ws.send_text("Connected to server 🚀")
        while True:
            await ws.send_text(
                f"Server running. Uptime: {time.time() - startup_timer.start_time:.2f}s"
            )
            await asyncio.sleep(5)


# ------------------- CORE APP -------------------

def create_api(app):
    from modules.api.api import Api
    from modules.call_queue import queue_lock

    api = Api(app, queue_lock)
    return api


def api_only():
    from fastapi import FastAPI
    from modules.shared_cmd_options import cmd_opts

    initialize.initialize()
    start_scheduler()

    app = FastAPI()
    initialize_util.setup_middleware(app)
    api = create_api(app)

    # New features
    add_healthcheck(app)
    add_control_endpoints(app)
    add_websocket(app)

    from modules import script_callbacks
    script_callbacks.before_ui_callback()
    script_callbacks.app_started_callback(None, app)

    print(f"Startup time: {startup_timer.summary()}.")
    api.launch(
        server_name=initialize_util.gradio_server_name(),
        port=cmd_opts.port if cmd_opts.port else 7861,
        root_path=f"/{cmd_opts.subpath}" if cmd_opts.subpath else ""
    )


def webui():
    from modules.shared_cmd_options import cmd_opts

    launch_api = cmd_opts.api
    initialize.initialize()
    start_scheduler()

    from modules import shared, ui_tempdir, script_callbacks, ui, progress, ui_extra_networks

    while 1:
        if shared.opts.clean_temp_dir_at_start:
            ui_tempdir.cleanup_tmpdr()
            startup_timer.record("cleanup temp dir")

        script_callbacks.before_ui_callback()
        startup_timer.record("scripts before_ui_callback")

        shared.demo = ui.create_ui()
        startup_timer.record("create ui")

        if not cmd_opts.no_gradio_queue:
            shared.demo.queue(64)

        gradio_auth_creds = list(initialize_util.get_gradio_auth_creds()) or None

        auto_launch_browser = False
        if os.getenv('SD_WEBUI_RESTARTING') != '1':
            if shared.opts.auto_launch_browser == "Remote" or cmd_opts.autolaunch:
                auto_launch_browser = True
            elif shared.opts.auto_launch_browser == "Local":
                auto_launch_browser = not cmd_opts.webui_is_non_local

        app, local_url, share_url = shared.demo.launch(
            share=cmd_opts.share,
            server_name=initialize_util.gradio_server_name(),
            server_port=cmd_opts.port,
            ssl_keyfile=cmd_opts.tls_keyfile,
            ssl_certfile=cmd_opts.tls_certfile,
            ssl_verify=cmd_opts.disable_tls_verify,
            debug=cmd_opts.gradio_debug,
            auth=gradio_auth_creds,
            inbrowser=auto_launch_browser,
            prevent_thread_lock=True,
            allowed_paths=cmd_opts.gradio_allowed_path,
            app_kwargs={
                "docs_url": "/docs",
                "redoc_url": "/redoc",
            },
            root_path=f"/{cmd_opts.subpath}" if cmd_opts.subpath else "",
        )

        startup_timer.record("gradio launch")

        app.user_middleware = [x for x in app.user_middleware if x.cls.__name__ != 'CORSMiddleware']
        initialize_util.setup_middleware(app)

        progress.setup_progress_api(app)
        ui.setup_ui_api(app)

        # New features
        add_healthcheck(app)
        add_control_endpoints(app)
        add_websocket(app)

        if launch_api:
            create_api(app)

        ui_extra_networks.add_pages_to_demo(app)
        startup_timer.record("add APIs")

        with startup_timer.subcategory("app_started_callback"):
            script_callbacks.app_started_callback(shared.demo, app)

        timer.startup_record = startup_timer.dump()
        print(f"Startup time: {startup_timer.summary()}.")

        try:
            while True:
                server_command = shared.state.wait_for_server_command(timeout=5)
                if server_command:
                    if server_command in ("stop", "restart"):
                        break
                    elif server_command == "reload_config":
                        reload_config()
                    else:
                        print(f"Unknown server command: {server_command}")
        except KeyboardInterrupt:
            print('Caught KeyboardInterrupt, stopping...')
            server_command = "stop"

        if server_command == "stop":
            print("Stopping server...")
            shared.demo.close()
            break

        os.environ.setdefault('SD_WEBUI_RESTARTING', '1')

        print('Restarting UI...')
        shared.demo.close()
        time.sleep(0.5)
        startup_timer.reset()
        script_callbacks.app_reload_callback()
        startup_timer.record("app reload callback")
        script_callbacks.script_unloaded_callback()
        startup_timer.record("scripts unloaded callback")
        initialize.initialize_rest(reload_script_modules=True)


if __name__ == "__main__":
    from modules.shared_cmd_options import cmd_opts

    if cmd_opts.nowebui:
        api_only()
    else:
        webui()
