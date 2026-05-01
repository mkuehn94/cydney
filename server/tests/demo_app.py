from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.app import app as production_app, lifespan
from server.tests.demo_data import install_demo_state


@asynccontextmanager
async def fixture_lifespan(instance):
    async with lifespan(instance):
        install_demo_state(instance)
        yield


app = FastAPI(lifespan=fixture_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# same endpoints as the real app, but with the demo data
app.router.routes.extend(production_app.router.routes)
