from functools import lru_cache
from typing import Dict, Optional

from fastapi import APIRouter, Body, Depends

from mod.llm import AppConfigService, HTTPMCPServerResponse, LLMConfig
from mod.mcp import MCPConfig
from api.res import Response


router = APIRouter(prefix="/llm", tags=["设置接口"])


@lru_cache
def get_app_config_service() -> AppConfigService:
    return AppConfigService()


@router.get(
    path="/llm",
    summary="获取 llm 配置信息",
    description="返回LLM提供商的base_url，api_key,model_name",
    response_model=Response[LLMConfig],
)
async def get_llm_config(
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[LLMConfig]:
    llm_config = app_config_service.get_llm_config()
    safe = LLMConfig(
        base_url=llm_config.base_url,
        api_key="***",
        model_name=llm_config.model_name,
    )
    return Response.success(data=safe)


@router.post(
    path="/llm",
    summary="更新 llm 配置信息",
    description="更新LLM提供商的base_url，api_key,model_name",
    response_model=Response[LLMConfig],
)
async def update_llm_config(
    new_llm_config: LLMConfig,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[LLMConfig]:
    update_llm_config = app_config_service.update_llm_config(new_llm_config)
    return Response.success(
        data=update_llm_config.model_dump(exclude={"api_key"}), msg="更新llm配置成功"
    )


@router.get(
    path="/mcp",
    response_model=Response,
    summary="获取MCP服务器工具列表",
    description="获取MCP服务器工具列表",
)
async def get_mcp_servers(
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response:
    mcp_servers = await app_config_service.get_mcp_servers()
    return Response.success(
        msg="获取mcp服务列表成功", data=HTTPMCPServerResponse(mcp_servers=mcp_servers)
    )


@router.post(
    path="/mcp",
    response_model=Response[Optional[Dict]],
    summary="新增MCP",
    description="新增MCP",
)
async def add_mcp_servers(
    mcp_config: MCPConfig,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.upset_mcp_servers(mcp_config)
    return Response.success("新增MCP成功")


@router.post(
    path="/mcp/{name}/delete",
    response_model=Response[Optional[Dict]],
    summary="删除MCP",
    description="删除MCP",
)
async def delete_mcp_servers(
    name: str,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.delete_mcp_servers(name)
    return Response.success("删除MCP成功")


@router.post(
    path="/mcp/{name}/enabled",
    response_model=Response[Optional[Dict]],
    summary="更新MCP启动状态",
    description="更新MCP启动状态",
)
async def set_mcp_enabled(
    name: str,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.set_mcp_enabled(name)
    return Response.success("更新MCP状态成功")


@router.get(
    path="/a2a",
    response_model=Response,
    summary="获取MCP服务器工具列表",
    description="获取MCP服务器工具列表",
)
async def get_a2a_servers(
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response:
    a2a_servers = await app_config_service.get_a2a_servers()
    return Response.success(msg="获取 A2A 列表成功", data=a2a_servers)


@router.post(
    path="/a2a",
    response_model=Response[Optional[Dict]],
    summary="新增A2A",
    description="新增A2A",
)
async def add_a2a_servers(
    base_url: str = Body(..., embed=True),
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.add_a2a_server(base_url)
    return Response(msg="新增A2A成功")


@router.post(
    path="/a2a/{id}/delete",
    response_model=Response[Optional[Dict]],
    summary="删除A2A",
    description="删除A2A",
)
async def delete_a2a_servers(
    id: str,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.remove_a2a_server(id)
    return Response.success(msg="删除A2A服务成功")


@router.post(
    path="/a2a/{id}/enabled",
    response_model=Response[Optional[Dict]],
    summary="更新A2A启动状态",
    description="更新A2A启动状态",
)
async def set_a2a_enabled(
    id: str,
    app_config_service: AppConfigService = Depends(get_app_config_service),
) -> Response[Optional[Dict]]:
    await app_config_service.set_a2a_server_enabled(id)
    return Response.success(msg="状态切换成功")
