"""网络与代理设置接口（「设置 → 网络」页的后端）。

三个端点（实现全部在 services/network_config.py，本文件只做参数进出）：
- GET  /network/config —— 当前配置 + 服务开关目录 + 镜像地址生效默认值；
- PUT  /network/config —— 保存并**立即生效**（代理路由热切换，无需重启）；
- POST /network/test   —— 按服务标签发一次最小探测请求，返回延迟或中文错误。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.network import (
    NetworkConfigPayload,
    NetworkConfigView,
    NetworkTestPayload,
    NetworkTestResult,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import demo as demo_service
from movieclaw_api.services import network_config
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/network", tags=["network"])


@router.get(
    "/config",
    response_model=ApiResponse[NetworkConfigView],
    summary="读取网络与代理配置",
    operation_id="net.show",
)
async def get_network_config(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[NetworkConfigView]:
    view = await network_config.build_config_view(session)
    if demo_service.is_demo_mode():
        # 公开演示站：代理地址可能带账号密码，访客（公开的超管）看不到
        view.proxy_url = ""
        view.env_proxy_detected = ""
    return ok(view)


@router.put(
    "/config",
    response_model=ApiResponse[NetworkConfigView],
    summary="保存网络与代理配置（立即生效，无需重启）",
    operation_id="net.set",
)
async def save_network_config(
    payload: NetworkConfigPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[NetworkConfigView]:
    return ok(await network_config.save_config(session, payload))


@router.post(
    "/test",
    response_model=ApiResponse[NetworkTestResult],
    summary="按服务做一次连通性测试（走当前保存的出口配置）",
    operation_id="net.test",
)
async def test_network_service(
    payload: NetworkTestPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[NetworkTestResult]:
    return ok(await network_config.test_service(session, payload.service.strip()))
