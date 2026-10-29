"""Small REST API for checking status and manual control.

If API_KEY is set in .env, the control (POST) endpoints require the header
`X-API-Key: <your key>`. Status and health are always open.
"""

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .controller import Controller


class Duration(BaseModel):
    seconds: Optional[float] = None
    minutes: Optional[float] = None

    def total_seconds(self) -> Optional[float]:
        if self.seconds is not None:
            return self.seconds
        if self.minutes is not None:
            return self.minutes * 60
        return None


def create_app(controller: Controller, api_key: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="VIP Vertical Farm - Timer")

    def require_key(x_api_key: Optional[str] = Header(default=None)):
        if api_key and x_api_key != api_key:
            raise HTTPException(401, "missing or wrong X-API-Key header")

    def run(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(404, f"unknown device (use one of: {', '.join(controller.devices)})")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status():
        return controller.status()

    @app.post("/api/{device}/on", dependencies=[Depends(require_key)])
    def turn_on(device: str, body: Duration = Duration()):
        return run(controller.set_override, device, True, body.total_seconds())

    @app.post("/api/{device}/off", dependencies=[Depends(require_key)])
    def turn_off(device: str, body: Duration = Duration()):
        return run(controller.set_override, device, False, body.total_seconds())

    @app.post("/api/{device}/auto", dependencies=[Depends(require_key)])
    def back_to_schedule(device: str):
        return run(controller.clear_override, device)

    return app
