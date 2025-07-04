"""Utility functions for database operations in Elasticsearch/OpenSearch.

This module provides utility functions for working with database operations
in Elasticsearch/OpenSearch, such as parameter validation.
"""

import logging
from typing import Dict, List, Union

from stac_fastapi.core.utilities import get_bool_env
from stac_fastapi.extensions.core.transaction.request import (
    PatchAddReplaceTest,
    PatchOperation,
    PatchRemove,
)
from stac_fastapi.sfeos_helpers.models.patch import ElasticPath, ESCommandSet


def validate_refresh(value: Union[str, bool]) -> str:
    """
    Validate the `refresh` parameter value.

    Args:
        value (Union[str, bool]): The `refresh` parameter value, which can be a string or a boolean.

    Returns:
        str: The validated value of the `refresh` parameter, which can be "true", "false", or "wait_for".
    """
    logger = logging.getLogger(__name__)

    # Handle boolean-like values using get_bool_env
    if isinstance(value, bool) or value in {
        "true",
        "false",
        "1",
        "0",
        "yes",
        "no",
        "y",
        "n",
    }:
        is_true = get_bool_env("DATABASE_REFRESH", default=value)
        return "true" if is_true else "false"

    # Normalize to lowercase for case-insensitivity
    value = value.lower()

    # Handle "wait_for" explicitly
    if value == "wait_for":
        return "wait_for"

    # Log a warning for invalid values and default to "false"
    logger.warning(
        f"Invalid value for `refresh`: '{value}'. Expected 'true', 'false', or 'wait_for'. Defaulting to 'false'."
    )
    return "false"


def merge_to_operations(data: Dict) -> List:
    """Convert merge operation to list of RF6902 operations.

    Args:
        data: dictionary to convert.

    Returns:
        List: list of RF6902 operations.
    """
    operations = []

    for key, value in data.copy().items():

        if value is None:
            operations.append(PatchRemove(op="remove", path=key))

        elif isinstance(value, dict):
            nested_operations = merge_to_operations(value)

            for nested_operation in nested_operations:
                nested_operation.path = f"{key}.{nested_operation.path}"
                operations.append(nested_operation)

        else:
            operations.append(PatchAddReplaceTest(op="add", path=key, value=value))

    return operations


def check_commands(
    commands: ESCommandSet,
    op: str,
    path: ElasticPath,
    from_path: bool = False,
) -> None:
    """Add Elasticsearch checks to operation.

    Args:
        commands (List[str]): current commands
        op (str): the operation of script
        path (Dict): path of variable to run operation on
        from_path (bool): True if path is a from path

    """
    if path.nest:
        commands.add(
            f"if (!ctx._source.containsKey('{path.nest}'))"
            f"{{Debug.explain('{path.nest} does not exist');}}"
        )

    if path.index or op in ["remove", "replace", "test"] or from_path:
        commands.add(
            f"if (!ctx._source{path.es_nest}.containsKey('{path.key}'))"
            f"{{Debug.explain('{path.key}  does not exist in {path.nest}');}}"
        )

    if from_path and path.index is not None:
        commands.add(
            f"if ((ctx._source{path.es_location} instanceof ArrayList"
            f" && ctx._source{path.es_location}.size() < {path.index})"
            f" || (!(ctx._source{path.es_location} instanceof ArrayList)"
            f" && !ctx._source{path.es_location}.containsKey('{path.index}')))"
            f"{{Debug.explain('{path.path} does not exist');}}"
        )


def remove_commands(commands: ESCommandSet, path: ElasticPath) -> None:
    """Remove value at path.

    Args:
        commands (List[str]): current commands
        path (ElasticPath): Path to value to be removed

    """
    if path.index is not None:
        commands.add(
            f"def {path.variable_name} = ctx._source{path.es_location}.remove({path.index});"
        )

    else:
        commands.add(
            f"def {path.variable_name} = ctx._source{path.es_nest}.remove('{path.key}');"
        )


def add_commands(
    commands: ESCommandSet,
    operation: PatchOperation,
    path: ElasticPath,
    from_path: ElasticPath,
    params: Dict,
) -> None:
    """Add value at path.

    Args:
        commands (List[str]): current commands
        operation (PatchOperation): operation to run
        path (ElasticPath): path for value to be added

    """
    if from_path is not None:
        value = (
            from_path.variable_name
            if operation.op == "move"
            else f"ctx._source.{from_path.es_path}"
        )
    else:
        value = f"params.{path.param_key}"
        params[path.param_key] = operation.value

    if path.index is not None:
        commands.add(
            f"if (ctx._source{path.es_location} instanceof ArrayList)"
            f"{{ctx._source{path.es_location}.{'add' if operation.op in ['add', 'move'] else 'set'}({path.index}, {value})}}"
            f"else{{ctx._source.{path.es_path} = {value}}}"
        )

    else:
        commands.add(f"ctx._source.{path.es_path} = {value};")


def test_commands(
    commands: ESCommandSet, operation: PatchOperation, path: ElasticPath, params: Dict
) -> None:
    """Test value at path.

    Args:
        commands (List[str]): current commands
        operation (PatchOperation): operation to run
        path (ElasticPath): path for value to be tested
    """
    value = f"params.{path.param_key}"
    params[path.param_key] = operation.value

    commands.add(
        f"if (ctx._source.{path.es_path} != {value})"
        f"{{Debug.explain('Test failed `{path.path}` | "
        f"{operation.json_value} != ' + ctx._source.{path.es_path});}}"
    )


def operations_to_script(operations: List) -> Dict:
    """Convert list of operation to painless script.

    Args:
        operations: List of RF6902 operations.

    Returns:
        Dict: elasticsearch update script.
    """
    commands: ESCommandSet = ESCommandSet()
    params: Dict = {}

    for operation in operations:
        path = ElasticPath(path=operation.path)
        from_path = (
            ElasticPath(path=operation.from_) if hasattr(operation, "from_") else None
        )

        check_commands(commands=commands, op=operation.op, path=path)
        if from_path is not None:
            check_commands(
                commands=commands, op=operation.op, path=from_path, from_path=True
            )

        if operation.op in ["remove", "move"]:
            remove_path = from_path if from_path else path
            remove_commands(commands=commands, path=remove_path)

        if operation.op in ["add", "replace", "copy", "move"]:
            add_commands(
                commands=commands,
                operation=operation,
                path=path,
                from_path=from_path,
                params=params,
            )

        if operation.op == "test":
            test_commands(
                commands=commands, operation=operation, path=path, params=params
            )

        source = "".join(commands)

    return {
        "source": source,
        "lang": "painless",
        "params": params,
    }
