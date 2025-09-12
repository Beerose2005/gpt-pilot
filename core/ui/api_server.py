import asyncio
import uuid
from typing import Awaitable, Callable, Dict, Optional
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.cli.helpers import (
    calculate_pr_changes,
    change_order_of_task,
    find_first_todo_task_index,
    find_task_by_id,
    insert_new_task,
    load_convo,
    print_convo,
)
from core.config.actions import CM_UPDATE_FILES
from core.db.models.chat_convo import ChatConvo
from core.db.models.chat_message import ChatMessage
from core.db.models.project_state import TaskStatus
from core.llm.convo import Convo
from core.log import get_logger
from core.state.state_manager import StateManager
from core.ui.ipc_client import MESSAGE_SIZE_LIMIT, Message, MessageType
from core.ui.virtual import VirtualUI

log = get_logger(__name__)


async def send_stream_chunk(writer: asyncio.StreamWriter, message_type, content_chunk, request_id):
    if not content_chunk:
        content_chunk = ""
    stream_message = Message(type=message_type, content={"chunk": content_chunk}, request_id=request_id)
    data = stream_message.to_bytes()
    writer.write(len(data).to_bytes(4))
    writer.write(data)
    await writer.drain()


class IPCServer:
    """
    IPC server for handling requests from external clients.
    """

    def __init__(self, host: str, port: int, state_manager: StateManager):
        """
        Initialize the IPC server.

        :param host: Host to bind to.
        :param port: Port to listen on.
        :param state_manager: State manager instance.
        """
        self.host = host
        self.port = port
        self.state_manager = state_manager
        self.server = None
        self.handlers: Dict[MessageType, Callable[[Message, asyncio.StreamWriter], Awaitable[None]]] = {}
        self._setup_handlers()
        self.convo_id = None

    def _setup_handlers(self):
        """Set up message handlers."""
        self.handlers[MessageType.EPICS_AND_TASKS] = self._handle_epics_and_tasks
        self.handlers[MessageType.CHAT_MESSAGE] = self._handle_chat_message
        self.handlers[MessageType.START_CHAT] = self._handle_start_chat
        self.handlers[MessageType.GET_CHAT_HISTORY] = self._handle_get_chat_history
        self.handlers[MessageType.PROJECT_INFO] = self._handle_project_info
        self.handlers[MessageType.KNOWLEDGE_BASE] = self._handle_knowledge_base
        self.handlers[MessageType.PROJECT_SPECS] = self._handle_project_specs
        self.handlers[MessageType.TASK_CONVO] = self._handle_task_convo
        self.handlers[MessageType.EDIT_SPECS] = self._handle_edit_specs
        self.handlers[MessageType.FILE_DIFF] = self._handle_file_diff
        self.handlers[MessageType.TASK_CURRENT_STATUS] = self._handle_current_task_status
        self.handlers[MessageType.TASK_ADD_NEW] = self._handle_add_new_task
        self.handlers[MessageType.TASK_START_OTHER] = self._handle_start_another_task

    async def start(self) -> bool:
        """
        Start the IPC server.

        :return: True if server started successfully, False otherwise.
        """
        try:
            self.server = await asyncio.start_server(
                self._handle_client,
                self.host,
                self.port,
                limit=MESSAGE_SIZE_LIMIT,
            )
            log.info(f"IPC server started on {self.host}:{self.port}")
            return True
        except (OSError, ConnectionError) as err:
            log.error(f"Failed to start IPC server: {err}")
            return False

    async def stop(self):
        """Stop the IPC server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            log.info(f"IPC server on {self.host}:{self.port} stopped")
            self.server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Handle client connection.

        :param reader: Stream reader.
        :param writer: Stream writer.
        """
        addr = writer.get_extra_info("peername")
        log.debug(f"New connection from {addr}")

        try:
            while True:
                # Read message length (4 bytes)
                length_bytes = await reader.readexactly(4)
                if not length_bytes:
                    break

                # Parse message length
                message_length = int.from_bytes(length_bytes)

                # Read message data
                data = await reader.readexactly(message_length)
                if not data:
                    break

                # Parse message
                try:
                    message = Message.from_bytes(data)
                    await self._process_message(message, writer)
                except ValidationError as err:
                    log.error(f"Invalid message format: {err}")
                    await self._send_error(writer, "Invalid message format")
                except ValueError as err:
                    log.error(f"Error decoding message: {err}")
                    await self._send_error(writer, "Error decoding message")

        except asyncio.IncompleteReadError:
            log.debug(f"Client {addr} disconnected")
        except (ConnectionResetError, BrokenPipeError) as err:
            log.debug(f"Connection to {addr} lost: {err}")
        finally:
            writer.close()
            await writer.wait_closed()
            log.debug(f"Connection to {addr} closed")

    async def _process_message(self, message: Message, writer: asyncio.StreamWriter):
        """
        Process incoming message.

        :param message: Incoming message.
        :param writer: Stream writer to send response.
        """
        log.debug(f"Received message of type {message.type} with request ID {message.request_id}")

        handler = self.handlers.get(message.type)
        if handler:
            await handler(message, writer)
        else:
            log.warning(f"No handler for message type {message.type}")
            request_id = getattr(message, "request_id", None)
            await self._send_error(writer, f"Unsupported message type: {message.type}", request_id)

    async def _send_response(self, writer: asyncio.StreamWriter, message: Message):
        """
        Send response to client.

        :param writer: Stream writer.
        :param message: Message to send.
        """
        data = message.to_bytes()
        try:
            writer.write(len(data).to_bytes(4, byteorder="big"))
            writer.write(data)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as err:
            log.error(f"Failed to send response: {err}")

    async def _add_to_chat_history(
        self, project_state_id: uuid.UUID, conversation: Convo, message: Message, assistant_message: str
    ):
        """
        Add new messages to chat history.

        :param project_state_id: Project state ID.
        :param user_message: User message.
        :param assistant_message: Assistant message.
        """
        user_message = message.content.get("message", "")

        try:
            session = self.state_manager.current_session
            if not session:
                log.error("No database session available")
                return

            # Check if convo exists for this convo id
            query = select(ChatConvo).where(ChatConvo.convo_id == self.convo_id)
            result = await session.execute(query)
            chat_convo = result.scalar_one_or_none()

            # Create new convo if needed
            if not chat_convo:
                chat_convo = ChatConvo(convo_id=self.convo_id, project_state_id=project_state_id)
                session.add(chat_convo)
                await session.flush()

            # Check if any ChatMessages exists with the convo id
            query = select(ChatMessage).where(ChatMessage.convo_id == chat_convo.convo_id)
            result = await session.execute(query)
            results = result.scalars().all()
            prev_message_id = None

            if results:
                prev_message_id = results[-1].id

            # Create new messages
            message_user = ChatMessage(
                convo_id=chat_convo.convo_id,
                message_type="user",
                message=conversation.messages[1]["content"] if prev_message_id is None else user_message,
                prev_message_id=prev_message_id,
            )
            session.add(message_user)
            await session.flush()

            message_assistant = ChatMessage(
                convo_id=chat_convo.convo_id,
                message_type="assistant",
                message=assistant_message,
                prev_message_id=message_user.id,
            )
            session.add(message_assistant)
            await session.flush()

            log.debug(f"Added chat message to database for project state {project_state_id}")

        except Exception as e:
            log.error(f"Error saving chat history: {e}")

    async def _send_streaming_response(self, writer: asyncio.StreamWriter, message: Message):
        """
        Send a streaming response to client.

        :param writer: Stream writer.
        :param message: Message with request id, content and message type.
        """
        request_id = message.request_id
        message_type = MessageType.CHAT_MESSAGE_RESPONSE

        project_state_id = self.state_manager.current_state.id

        class ChatAgent(BaseAgent):
            agent_type = "chat-agent"
            display_name = "Chat Agent"

            async def stream_handler(self, content_chunk: str):
                await send_stream_chunk(writer, message_type, content_chunk, request_id)

            async def generate_prompt(self, convo_id: uuid.UUID, user_msg: str) -> Convo:
                bug_hunt_cycle_user_feedback = None
                command_run = None
                testing_instructions = None
                human_intervention = None
                task_description = None
                initial_description = None

                # get the ChatConvo for the convo_id, then use its project state id to get the wanted project state
                state = await self.state_manager.get_project_state_for_convo_id(convo_id)

                if state and self.current_state.id != state.id:
                    log.debug(f"State {state.id} does not match current state")
                else:
                    state = self.current_state

                if state.epics and state.epics[0].get("description", None) is not None:
                    initial_description = state.epics[0]["description"]

                if state.current_task and state.current_task.get("description", None) is not None:
                    task_description = state.current_task["description"]

                if state.iterations:
                    bug_hunt_cycle_user_feedback = state.iterations[-1].get("user_feedback", None)

                if state.current_task and state.current_task.get("test_instructions", None):
                    testing_instructions = state.current_task.get("test_instructions")

                if (
                    state.steps
                    and state.steps[-1].get("command", {}) != {}
                    and state.steps[-1].get("completed", False) is False
                ):
                    command_run = state.steps[-1].get("command", {}).get("command", "")

                if (
                    state.steps
                    and state.steps[-1].get("human_intervention_description", None) is not None
                    and state.steps[-1].get("completed", False) is False
                ):
                    human_intervention = state.steps[-1].get("human_intervention_description", None)

                return AgentConvo(self).template(
                    "chat",
                    initial_description=initial_description,
                    task_description=task_description,
                    bug_hunt_cycle_user_feedback=bug_hunt_cycle_user_feedback,
                    testing_instructions=testing_instructions,
                    command_run=command_run,
                    human_intervention=human_intervention,
                    user_input=user_msg,
                )

            async def generate_convo(self, convo_id: uuid.UUID, chat_history: list) -> Convo:
                # chat_history = await self.state_manager.get_chat_history(convo_id)
                conversation = await self.generate_prompt(
                    convo_id=convo_id, user_msg=message.content.get("message", "")
                )

                for chat_msg in chat_history:
                    if chat_msg.get("role", "") == "user":
                        conversation = conversation.user(chat_msg.get("content", ""))
                    if chat_msg.get("role", "") == "assistant":
                        conversation = conversation.assistant(chat_msg.get("content", ""))

                if len(conversation.messages) > 6:
                    conversation.slice(2, 4)

                return conversation

            async def run(self):
                log.debug("Chat agent started.")
                llm = self.get_llm(stream_output=True)

                convo_id = uuid.UUID(message.content.get("convoId", ""))
                chat_history = message.content.get("chatHistory", [])
                conversation = await self.generate_convo(convo_id, chat_history)
                return conversation, await llm(conversation)

        try:
            convo, response = await ChatAgent(state_manager=self.state_manager, ui=VirtualUI(inputs=[])).run()
            await self._add_to_chat_history(project_state_id, convo, message, str(response))

            # Send final message
            await send_stream_chunk(writer, message_type, None, request_id)

        except (ConnectionResetError, BrokenPipeError) as err:
            log.error(f"Failed to send streaming response: {err}", exc_info=True)
        except Exception as err:
            log.error(f"Error during streaming response: {err}", exc_info=True)

    async def _send_error(self, writer: asyncio.StreamWriter, error_message: str, request_id: Optional[str] = None):
        """
        Send error response to client.

        :param writer: Stream writer.
        :param error_message: Error message.
        :param request_id: Optional request ID to include in the response.
        """
        message = Message(type=MessageType.RESPONSE, content={"error": error_message}, request_id=request_id)
        await self._send_response(writer, message)

    async def _handle_start_chat(self, message: Message, writer: asyncio.StreamWriter):
        """
        Handle request for starting chat.

        :param message: Request message.
        :param writer: Stream writer to send response.
        """
        log.debug("start chat")
        self.convo_id = uuid.uuid4()
        response = Message(
            type=MessageType.START_CHAT,
            content={"convoId": self.convo_id},
            request_id=message.request_id,
        )
        await self._send_response(writer, response)

    async def _handle_get_chat_history(self, message: Message, writer: asyncio.StreamWriter):
        log.debug("get chat history")

        convo_id = uuid.UUID(message.content.get("convoId", ""))
        if not convo_id:
            await self._send_error(writer, "convoId is required", message.request_id)
            return

        # Get chat history for the given convo_id
        db_chat_history = await self.state_manager.get_chat_history(convo_id)
        chat_history = []

        for chat_msg in db_chat_history:
            chat_history.append(
                {
                    "role": chat_msg.message_type,
                    "content": chat_msg.message,
                    "id": chat_msg.id,
                }
            )
        response = Message(
            type=MessageType.GET_CHAT_HISTORY,
            content={"chatHistory": chat_history},
            request_id=message.request_id,
        )
        await self._send_response(writer, response)

    async def _handle_epics_and_tasks(self, message: Message, writer: asyncio.StreamWriter):
        """
        Handle request for epics and tasks.

        :param message: Request message.
        :param writer: Stream writer to send response.
        """
        try:
            # Get current state
            epics_and_tasks = await self.state_manager.get_all_epics_and_tasks(self.state_manager.branch.id)

            # Send response with the same request_id from the incoming message
            response = Message(
                type=MessageType.EPICS_AND_TASKS,
                content={"epicsAndTasks": epics_and_tasks},
                request_id=message.request_id,  # Include the request_id from the incoming message
            )
            log.debug(f"Sending epics and tasks response with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling epics and tasks request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_project_info(self, message: Message, writer: asyncio.StreamWriter):
        try:
            project_details = self.state_manager.get_project_info()
            response = Message(
                type=MessageType.PROJECT_INFO,
                content=project_details,
                request_id=message.request_id,
            )
            log.debug(f"Sending project info with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling project info request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_knowledge_base(self, message: Message, writer: asyncio.StreamWriter):
        try:
            state = self.state_manager.current_state
            if message.content and message.content.get("projectStateId", None) is not None:
                state = await self.state_manager.get_project_state_by_id(
                    uuid.UUID(message.content.get("projectStateId", ""))
                )

            if not state:
                await self._send_error(writer, "Project state not found", message.request_id)
                return

            # Convert KnowledgeBase object to dictionary for JSON serialization
            knowledge_base_dict = {
                "pages": state.knowledge_base.pages,
                "apis": state.knowledge_base.apis,
                "user_options": state.knowledge_base.user_options,
                "utility_functions": state.knowledge_base.utility_functions,
            }

            response = Message(
                type=MessageType.KNOWLEDGE_BASE,
                content={
                    "projectStateId": state.id,
                    "knowledgeBase": knowledge_base_dict,
                },
                request_id=message.request_id,
            )
            log.debug(f"Sending knowledge base with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling knowledge base request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_project_specs(self, message: Message, writer: asyncio.StreamWriter):
        try:
            current_state = self.state_manager.current_state
            next_state = self.state_manager.next_state

            if next_state.specification and next_state.specification.description:
                project_state_id = next_state.id
                project_specification = next_state.specification.description
            else:
                project_state_id = current_state.id
                project_specification = current_state.specification.description

            response = Message(
                type=MessageType.PROJECT_SPECS,
                content={
                    "projectStateId": project_state_id,
                    "projectSpecification": project_specification,
                },
                request_id=message.request_id,
            )
            log.debug(f"Sending knowledge base with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling knowledge base request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_current_task_status(self, message: Message, writer: asyncio.StreamWriter):
        # extension needs to know whether the user is currently in the middle of implementing a task or not
        # if the user is in the middle of implementing a task, the extension should ask the user whether he wants to
        # mark current task as DONE or revert the current task progress ("question" is True)
        # otherwise, the extension should just start a new task without asking the user anything ("question" is False)
        try:
            state = self.state_manager.current_state

            if state.steps:
                question = True
            else:
                question = False

            response = Message(
                type=MessageType.TASK_CURRENT_STATUS,
                content={
                    "projectStateId": state.id,
                    "question": question,
                    "currentTask": state.current_task,
                },
                request_id=message.request_id,
            )
            log.debug(f"Sending current task status with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling current task status request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_add_new_task(self, message: Message, writer: asyncio.StreamWriter):
        # if the extension sent "add", just add a new task in the tasks array before the first todo task
        # if the extension sent "done", mark the current task as done and then do above^
        # if the extension sent "revert", reload the project state like in "redo task" implementation", then do above ^^
        try:
            if not message.content or "action" not in message.content or "taskDescription" not in message.content:
                await self._send_error(writer, "action and taskDescription are required", message.request_id)
                return

            action = message.content["action"]
            log.debug(f"action: {action}")

            current_state = self.state_manager.current_state
            next_state = self.state_manager.next_state

            # todo what if the project state changes between the get current task status and this request?
            #  should we just undo the task progress?

            if action == "done":
                next_state.current_task["status"] = TaskStatus.DOCUMENTED

            elif action == "revert":
                project_state = await self.state_manager.get_project_state_for_redo_task(current_state)

                if project_state is not None:
                    await self.state_manager.load_project(
                        branch_id=project_state.branch_id, step_index=project_state.step_index
                    )
                    await self.state_manager.restore_files()
                current_state = self.state_manager.current_state
                next_state = self.state_manager.next_state

            insert_new_task(
                next_state.tasks,
                {
                    "id": uuid4().hex,
                    "description": message.content["taskDescription"],
                    "instructions": None,
                    "pre_breakdown_testing_instructions": None,
                    "status": TaskStatus.TODO,
                    "sub_epic_id": current_state.tasks[0].get("sub_epic_id", 1) if current_state.tasks else 1,
                    "user_added_subsequently": True,  # this is how we know to skip the question if the user wants to start the task
                },
            )

            response = Message(
                type=MessageType.TASK_CURRENT_STATUS,
                content={
                    "projectStateId": self.state_manager.current_state.id,
                    # "currentTask": next_state.current_task, #todo idk
                },
                request_id=message.request_id,
            )
            log.debug(f"Sending add new task response with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling add new task request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_start_another_task(self, message: Message, writer: asyncio.StreamWriter):
        # receive task id to start before the current one?
        current_state = self.state_manager.current_state
        next_state = self.state_manager.next_state

        try:
            if not message.content or "taskId" not in message.content:
                await self._send_error(writer, "taskId required", message.request_id)
                return

            task_to_insert = find_task_by_id(current_state.tasks, message.content["taskId"])

            if not task_to_insert:
                await self._send_error(writer, f"Task with taskId {message.request_id} not found")
                return

            # try to reshuffle the tasks so that the task with the given id is the first one in order
            index = find_first_todo_task_index(current_state.tasks)

            if index == -1 or len(current_state.tasks) == 0:
                await self._send_error(writer, "No tasks available", message.request_id)
                return

            change_order_of_task(next_state.tasks, task_to_insert, index)

            response = Message(
                type=MessageType.TASK_START_OTHER,
                content={
                    "projectStateId": self.state_manager.current_state.id,
                },
                request_id=message.request_id,
            )
            log.debug(f"Sending add new task response with request_id: {message.request_id}")
            await self._send_response(writer, response)

        except Exception as e:
            log.error(f"Error handling start another task request: {e}")
            await self._send_error(writer, f"Internal server error: {str(e)}", message.request_id)

    async def _handle_chat_message(self, message: Message, writer: asyncio.StreamWriter):
        """
        Handle chat message with streaming response.

        :param message: Request message.
        :param writer: Stream writer to send response.
        """
        try:
            chat_message = message.content.get("message", "")
            if not chat_message:
                raise ValueError("Chat message is empty")

            # Stream the response
            await self._send_streaming_response(
                writer,
                message,
            )

        except Exception as err:
            log.error(f"Error handling chat message request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_task_convo(self, message: Message, writer: asyncio.StreamWriter):
        """
        Handle task conversation request.

        :param message: Request message.
        :param writer: Stream writer to send response.
        """
        log.debug("Got _handle_task_convo request")
        try:
            task_id = message.content.get("task_id", "")
            if task_id:
                task_id = uuid.UUID(task_id)
            start_project_id = uuid.UUID(message.content.get("start_id", ""))
            end_project_id = uuid.UUID(message.content.get("end_id", ""))
            log.debug(f"task_id: {task_id}, start_project_id: {start_project_id}, end_project_id: {end_project_id}")

            if start_project_id and end_project_id:
                project_states = await self.state_manager.get_project_states_in_between(
                    start_project_id, end_project_id
                )
            elif task_id:
                project_states = await self.state_manager.get_task_conversation_project_states(task_id)
            else:
                await self._send_error(writer, "task_id or (start_id and end_id) required", message.request_id)

            convo = await load_convo(sm=self.state_manager, project_states=project_states)

            response = Message(
                type=MessageType.TASK_CONVO,
                content={
                    "taskConvo": await print_convo(self.state_manager.ui, convo),
                    "project_state_id": start_project_id,
                },
                request_id=message.request_id,
            )
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling task convo request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_edit_specs(self, message: Message, writer: asyncio.StreamWriter):
        """
        Handle request to edit project specifications.

        :param message: Request message.
        :param writer: Stream writer to send response.
        """
        current_state = self.state_manager.current_state

        try:
            new_spec_desc = message.content.get("specification", "")
            if not new_spec_desc:
                await self._send_error(writer, "specification is required", message.request_id)
                return

            spec = current_state.specification
            spec.description = new_spec_desc
            await self.state_manager.update_specification(spec)

            response = Message(
                type=MessageType.EDIT_SPECS,
                content={
                    "projectStateId": current_state.id,
                    "specification": spec.description,
                },
                request_id=message.request_id,
            )
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling edit specs request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)

    async def _handle_file_diff(self, message: Message, writer: asyncio.StreamWriter):
        log.debug("Got _handle_file_diff request with message: %s", message)

        try:
            task_id = uuid.UUID(message.content.get("taskId", ""))
            type = message.content.get("type", "")

            if not task_id or not type:
                await self._send_error(writer, "taskId and type are required", message.request_id)
                return

            project_states = await self.state_manager.get_task_conversation_project_states(task_id)
            convo = await load_convo(sm=self.state_manager, project_states=project_states)

            if type == "task":
                filtered = list(filter(lambda x: x.get("action", "") == CM_UPDATE_FILES, convo))
            elif type == "bugHunter":
                filtered = list(
                    filter(
                        lambda x: x.get("action", "") == CM_UPDATE_FILES
                        and len(x.get("files", [])) > 0
                        and x["files"][0].get("bug_hunter", False) is True,
                        convo,
                    )
                )
            else:
                await self._send_error(writer, "Invalid type. Must be 'task' or 'bugHunter'", message.request_id)
                return

            file_diff = calculate_pr_changes(filtered)

            response = Message(
                type=MessageType.FILE_DIFF,
                content={"fileDiff": file_diff},
                request_id=message.request_id,
            )
            await self._send_response(writer, response)

        except Exception as err:
            log.error(f"Error handling file diff for task request: {err}", exc_info=True)
            await self._send_error(writer, f"Internal server error: {str(err)}", message.request_id)
