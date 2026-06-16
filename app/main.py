from fastapi import FastAPI
from app.api.routes.auth import router as auth_router
from app.api.routes.jobs import router as jobs_router
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
configure_logging(); log=get_logger(__name__)
def create_app():
    app=FastAPI(title='Cloud Resource Management API')
    register_exception_handlers(app); app.include_router(auth_router); app.include_router(jobs_router)
    @app.on_event('startup')
    async def startup(): log.info('app startup')
    @app.get('/health')
    async def health(): return {'status':'ok'}
    return app
app=create_app()
