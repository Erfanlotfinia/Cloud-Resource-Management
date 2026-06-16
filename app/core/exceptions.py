from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int): self.code=code; self.message=message; self.status_code=status_code

def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={'error': {'code': exc.code, 'message': exc.message}})
