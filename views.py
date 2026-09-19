from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

pocketmoney_generic_router = APIRouter(tags=["pocketmoney"])


@pocketmoney_generic_router.get(
    "/",
    description="PocketMoney extension dashboard",
    response_class=HTMLResponse,
)
async def index(
    request: Request,
    user: User = Depends(check_user_exists),
):
    return template_renderer(["pocketmoney/templates"]).TemplateResponse(
        request,
        "pocketmoney/index.html",
        {"user": user.json()},
    )
