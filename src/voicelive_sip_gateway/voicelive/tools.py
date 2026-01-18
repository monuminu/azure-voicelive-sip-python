"""Function calling/tool implementation for Azure Voice Live."""
from __future__ import annotations

import json
import datetime
from typing import Any, Callable, Dict, Mapping, Optional, Union

from azure.ai.voicelive.models import (
    FunctionTool,
    Tool,
    FunctionCallOutputItem,
    ServerEventType,
    ServerEventConversationItemCreated,
    ServerEventResponseFunctionCallArgumentsDone,
    ResponseFunctionCallItem,
    ItemType,
)
from structlog.stdlib import BoundLogger
import structlog
import asyncio


async def _wait_for_event(conn, wanted_types: set, timeout_s: float = 10.0):
    """Wait until we receive any event whose type is in wanted_types."""

    async def _next():
        while True:
            evt = await conn.recv()
            if evt.type in wanted_types:
                return evt

    return await asyncio.wait_for(_next(), timeout=timeout_s)


class ToolHandler:
    """Handles function calling/tool execution for Voice Live conversations."""

    def __init__(self) -> None:
        self._logger: BoundLogger = structlog.get_logger(__name__)

        # Map function names to their implementations
        self.available_functions: Dict[
            str, Callable[[Union[str, Mapping[str, Any]]], Mapping[str, Any]]
        ] = {
            "get_current_time": self.get_current_time,
            "get_current_weather": self.get_current_weather,
        }

    def get_tools(self) -> list[Tool]:
        """Return the list of function tools available for the conversation."""
        function_tools: list[Tool] = [
            FunctionTool(
                name="get_current_time",
                description="Get the current time",
                parameters={
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "The timezone to get the current time for, e.g., 'UTC', 'local'",
                        }
                    },
                    "required": [],
                },
            ),
            FunctionTool(
                name="get_current_weather",
                description="Get the current weather in a given location",
                parameters={
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g., 'San Francisco, CA'",
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "The unit of temperature to use (celsius or fahrenheit)",
                        },
                    },
                    "required": ["location"],
                },
            ),
        ]
        return function_tools

    def get_current_time(
        self, arguments: Optional[Union[str, Mapping[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Get the current time in the specified timezone."""
        # Parse arguments if provided as string
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            args = {}

        timezone = args.get("timezone", "local")
        now = datetime.datetime.now()

        if timezone.lower() == "utc":
            now = datetime.datetime.now(datetime.timezone.utc)
            timezone_name = "UTC"
        else:
            timezone_name = "Local"

        formatted_time = now.strftime("%I:%M:%S %p")
        formatted_date = now.strftime("%A, %B %d, %Y")

        self._logger.info(
            "tool.executed",
            function="get_current_time",
            timezone=timezone_name,
            time=formatted_time,
        )

        return {
            "time": formatted_time,
            "date": formatted_date,
            "timezone": timezone_name,
        }

    def get_current_weather(
        self, arguments: Optional[Union[str, Mapping[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Get the current weather for a given location (mock implementation)."""
        # Parse arguments if provided as string
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                self._logger.error("tool.parse_error", arguments=arguments)
                return {"error": "Invalid arguments"}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return {"error": "No arguments provided"}

        location = args.get("location", "Unknown")
        unit = args.get("unit", "celsius")

        # Mock weather data
        mock_weather = {
            "location": location,
            "temperature": 22 if unit == "celsius" else 72,
            "unit": unit,
            "condition": "Partly Cloudy",
            "humidity": 65,
            "wind_speed": 10,
        }

        self._logger.info(
            "tool.executed",
            function="get_current_weather",
            location=location,
            unit=unit,
        )

        return mock_weather

    async def handle_function_call(self, event, connection) -> None:
        """
        Handle function call using the improved pattern from Azure sample.

        Note: This method CANNOT use _wait_for_event because the caller is already
        iterating over the connection. Instead, the caller must pass us the
        RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE event when it arrives.

        Args:
            event: ServerEventConversationItemCreated with ResponseFunctionCallItem
            connection: The Voice Live connection object
        """
        # Validate the event structure
        if not isinstance(event, ServerEventConversationItemCreated):
            self._logger.error("tool.invalid_event", expected="ServerEventConversationItemCreated")
            return

        if not isinstance(event.item, ResponseFunctionCallItem):
            self._logger.error("tool.invalid_item", expected="ResponseFunctionCallItem")
            return

        function_call_item = event.item
        function_name = function_call_item.name
        call_id = function_call_item.call_id
        previous_item_id = function_call_item.id

        self._logger.info(
            "tool.function_call_started",
            function=function_name,
            call_id=call_id,
        )

        # Store the function call info - caller will complete it when args arrive
        return {
            "function_name": function_name,
            "call_id": call_id,
            "previous_item_id": previous_item_id,
        }

    async def execute_function_call(
        self, function_name: str, call_id: str, arguments: str, previous_item_id: str, connection
    ) -> None:
        """Execute a function call and send results back."""
        try:
            # Execute the function if we have it
            if function_name in self.available_functions:
                self._logger.info("tool.executing", function=function_name, arguments=arguments)
                result = self.available_functions[function_name](arguments)

                # Create function call output item
                function_output = FunctionCallOutputItem(
                    call_id=call_id,
                    output=json.dumps(result),
                )

                # Send the result back to the conversation
                await connection.conversation.item.create(
                    previous_item_id=previous_item_id,
                    item=function_output,
                )

                self._logger.info("tool.result_sent", function=function_name, result=result)

                # Create a new response to process the function result
                await connection.response.create()

                self._logger.info("tool.function_call_completed", function=function_name, call_id=call_id)
            else:
                self._logger.error("tool.function_not_found", function=function_name)

        except Exception as e:
            self._logger.error(
                "tool.execution_error",
                function=function_name,
                error=str(e),
            )
