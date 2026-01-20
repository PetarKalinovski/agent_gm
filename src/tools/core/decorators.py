"""Tool decorators for reducing boilerplate."""

from functools import wraps
from typing import Callable, Any, TypeVar

from strands import tool

from src.core.results import Result
from src.repositories.unit_of_work import UnitOfWork

T = TypeVar('T')


def game_tool(
    entity_type: str | None = None,
    entity_id_param: str | None = None,
    requires_entity: bool = True,
    transactional: bool = True,
):
    """Decorator for game tools with automatic session and validation handling.

    Wraps a function to automatically:
    - Create a UnitOfWork context
    - Fetch and validate entity if specified
    - Handle Result conversion for tool responses
    - Commit/rollback transactions

    Args:
        entity_type: Type of primary entity ("player", "npc", "location").
        entity_id_param: Name of the ID parameter (defaults to "{entity_type}_id").
        requires_entity: Whether to auto-fetch and validate entity exists.
        transactional: Whether to auto-commit on success.

    Example:
        @game_tool(entity_type="npc", requires_entity=True)
        @tool
        def update_npc_mood(npc_id: str, new_mood: str, _uow=None, _entity=None) -> dict:
            _entity.current_mood = new_mood
            return {"success": True, "npc_name": _entity.name, "new_mood": new_mood}
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> dict[str, Any]:
            # Determine entity ID parameter name
            id_param = entity_id_param or (f"{entity_type}_id" if entity_type else None)

            with UnitOfWork() as uow:
                # Auto-fetch entity if required
                if requires_entity and entity_type and id_param:
                    entity_id = kwargs.get(id_param)
                    if entity_id:
                        repo = getattr(uow, f"{entity_type}s", None)
                        if repo:
                            result = repo.get_by_id(entity_id)
                            if not result.success:
                                return result.to_tool_response()
                            kwargs['_entity'] = result.data

                kwargs['_uow'] = uow

                # Call the actual function
                try:
                    result = func(*args, **kwargs)

                    # Handle Result objects
                    if isinstance(result, Result):
                        if transactional and result.success:
                            uow.commit()
                        return result.to_tool_response()

                    # Handle dict returns (backward compatibility)
                    if isinstance(result, dict):
                        if transactional:
                            uow.commit()
                        return result

                    # Wrap other returns
                    if transactional:
                        uow.commit()
                    return {"success": True, "data": result}

                except Exception as e:
                    return {"success": False, "error": str(e), "error_code": "TOOL_ERROR"}

        return wrapper
    return decorator


def read_tool(entity_type: str | None = None, entity_id_param: str | None = None):
    """Decorator for read-only tools.

    Shorthand for @game_tool with transactional=False.

    Args:
        entity_type: Type of entity to fetch.
        entity_id_param: Name of the ID parameter.

    Example:
        @read_tool(entity_type="npc")
        @tool
        def get_npc_details(npc_id: str, _entity=None) -> dict:
            return {"id": _entity.id, "name": _entity.name}
    """
    return game_tool(
        entity_type=entity_type,
        entity_id_param=entity_id_param,
        requires_entity=entity_type is not None,
        transactional=False,
    )


def write_tool(entity_type: str | None = None, entity_id_param: str | None = None):
    """Decorator for write tools with automatic transaction.

    Shorthand for @game_tool with transactional=True.

    Args:
        entity_type: Type of entity to fetch.
        entity_id_param: Name of the ID parameter.

    Example:
        @write_tool(entity_type="npc")
        @tool
        def update_npc_status(npc_id: str, status: str, _entity=None) -> dict:
            _entity.status = status
            return {"success": True, "npc_name": _entity.name}
    """
    return game_tool(
        entity_type=entity_type,
        entity_id_param=entity_id_param,
        requires_entity=entity_type is not None,
        transactional=True,
    )


def with_uow(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """Simple decorator that provides a UnitOfWork to the function.

    For tools that need database access but don't fit the entity pattern.

    Example:
        @with_uow
        @tool
        def complex_query(player_id: str, _uow=None) -> dict:
            # Access multiple repositories
            player = _uow.players.get_by_id(player_id)
            npcs = _uow.npcs.get_at_location(player.data.current_location_id)
            return {"player": player.data, "npcs": npcs.data}
    """
    @wraps(func)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        with UnitOfWork() as uow:
            kwargs['_uow'] = uow
            try:
                result = func(*args, **kwargs)
                if isinstance(result, Result):
                    if result.success:
                        uow.commit()
                    return result.to_tool_response()
                if isinstance(result, dict):
                    uow.commit()
                    return result
                uow.commit()
                return {"success": True, "data": result}
            except Exception as e:
                return {"success": False, "error": str(e), "error_code": "TOOL_ERROR"}
    return wrapper
