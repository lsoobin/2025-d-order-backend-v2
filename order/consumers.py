import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.utils import timezone
from datetime import timedelta
import logging

try:
    from manager.models import Manager
    from booth.models import Table
except ImportError as e:

    logging.critical(f"Critical: Failed to import models at the top of consumers.py: {e}")

logger = logging.getLogger(__name__)


# ORM → async safe (thread_sensitive=True 로 고정)
@sync_to_async(thread_sensitive=True)
def get_manager_by_user(user):
    try:
        manager = Manager.objects.get(user=user)
        logger.debug(f"Found manager: {manager}")
        return manager
    except Manager.DoesNotExist:
        logger.warning(f"No Manager for user: {user} found.")
        return None
    except Exception as e:
        logger.error(f"Error in get_manager_by_user for user {user}: {e}", exc_info=True)
        raise


@sync_to_async(thread_sensitive=True)
def get_table_statuses(user):
    try:
        manager = Manager.objects.get(user=user)
    except Manager.DoesNotExist:
        logger.warning(f"No Manager found for user {user} in get_table_statuses. Returning empty list.")
        return []
    except Exception as e:
        logger.error(f"Error fetching manager in get_table_statuses for user {user}: {e}", exc_info=True)
        return []

    try:
        if not hasattr(manager, 'booth') or manager.booth is None:
            logger.error(f"Manager {manager} has no associated booth when trying to get table statuses.")
            return []

        tables = list(Table.objects.filter(booth=manager.booth))
        result = []
        for table in tables:
            remaining_minutes = None
            is_expired = False

            if table.activated_at:
                elapsed = timezone.now() - table.activated_at
                limit = timedelta(hours=manager.table_limit_hours)
                remaining_minutes = max(0, int((limit - elapsed).total_seconds() // 60))
                if elapsed > limit:
                    is_expired = True

            result.append({
                "tableNumber": table.table_num,
                "status": table.status,
                "activatedAt": table.activated_at.isoformat() if table.activated_at else None,
                "remainingMinutes": remaining_minutes,
                "expired": is_expired
            })
        return result
    except Exception as e:
        logger.error(f"Error fetching or processing table statuses for manager {manager}: {e}", exc_info=True)
        return []


async def send_error_and_close(self, code, message):
    valid_close_code = code
    # 웹소켓 표준에 따라 허용되는 종료 코드 범위 내로 조정
    if not (1000 <= code <= 1014 or 3000 <= code <= 4999):
        valid_close_code = 4000 # 사용자 정의 오류 코드 범위 내의 기본값

    try:
        await self.accept() # 연결 수락 시도
        logger.info(f"Accepted connection to send error code {code} and message '{message}'.")
        await self.send(text_data=json.dumps({
            "type": "ERROR",
            "code": code, # 클라이언트에는 원래 코드 전송
            "message": message
        }))
        logger.info(f"Sent error message to client. Closing connection with valid code {valid_close_code}.")
        await self.close(code=valid_close_code) # 웹소켓 종료 시에는 유효한 코드 사용
    except Exception as e:
        logger.critical(f"Failed to send error message or close connection gracefully: {e}", exc_info=True)


# 주문 웹소켓
class OrderConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("OrderConsumer: Connection attempt started.")
        user = self.scope.get("user")
        
        if not user or not user.is_authenticated:
            logger.warning("OrderConsumer: Connection rejected. User not authenticated or invalid.")
            return await send_error_and_close(self, 4001, "인증 실패: 유효하지 않은 사용자")

        try:
            manager = await get_manager_by_user(user)
            if not manager:
                logger.error(f"OrderConsumer: Connection rejected. Manager not found for user {user.id}.")
                return await send_error_and_close(self, 4003, "Manager 정보를 찾을 수 없습니다.")

            if not hasattr(manager, 'booth') or manager.booth is None:
                logger.error(f"OrderConsumer: Connection rejected. Manager {manager.id} has no associated booth.")
                return await send_error_and_close(self, 4004, "Manager에 연결된 부스 정보가 없습니다.")
            
            self.room_group_name = f"booth_{manager.booth.id}_orders"
            await self.accept()
            logger.info(f"OrderConsumer: Connection accepted for booth {manager.booth.id}.")
            
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            logger.info(f"OrderConsumer: User {user.id} added to channel group '{self.room_group_name}'.")

        except Exception as e:
            logger.error(f"OrderConsumer: Unexpected error during connection for user {user.id}: {e}", exc_info=True)
            try:
                await self.accept()
                await self.send(text_data=json.dumps({
                    "type": "ERROR",
                    "code": 5000,
                    "message": f"서버 연결 중 예기치 않은 오류 발생: {e.__class__.__name__}"
                }))
                await self.close(code=5000)
                logger.warning(f"OrderConsumer: Attempted to send error and close after an unexpected exception.")
            except Exception as close_e:
                logger.critical(f"OrderConsumer: Failed to send error or close connection after critical exception: {close_e}", exc_info=True)


    async def disconnect(self, close_code):
        logger.info(f"OrderConsumer: Disconnection initiated with code {close_code}.")
        if hasattr(self, 'room_group_name') and self.channel_layer:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            logger.info(f"OrderConsumer: User removed from channel group '{self.room_group_name}'.")
        else:
            logger.warning("OrderConsumer: room_group_name or channel_layer not defined during disconnect. Clean-up skipped.")

    async def receive(self, text_data):
        logger.debug(f"OrderConsumer: Received raw data: {text_data}")
        try:
            data = json.loads(text_data)
            if data.get("type") == "NEW_ORDER":
                if hasattr(self, 'room_group_name') and self.channel_layer:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "new_order",
                            "data": data["data"]
                        }
                    )
                    logger.debug(f"OrderConsumer: Sent NEW_ORDER to group '{self.room_group_name}'.")
                else:
                    logger.error("OrderConsumer: room_group_name or channel_layer not available to send NEW_ORDER.")
            else:
                logger.warning(f"OrderConsumer: Unknown message type received: '{data.get('type') or 'N/A'}'")
        except json.JSONDecodeError:
            logger.error(f"OrderConsumer: Failed to decode JSON from received data: '{text_data}'", exc_info=True)
        except Exception as e:
            logger.error(f"OrderConsumer: Error during receive processing: {e}", exc_info=True)

    async def new_order(self, event):
        logger.debug(f"OrderConsumer: Handling 'new_order' event for sending: {event.get('data')}")
        await self.send(text_data=json.dumps({
            "type": "NEW_ORDER",
            "data": event["data"]
        }))


# 직원 호출 웹소켓
class CallStaffConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("CallStaffConsumer: Connection attempt started.")
        user = self.scope.get("user")
        
        if not user or not user.is_authenticated:
            logger.warning("CallStaffConsumer: Connection rejected. User not authenticated or invalid.")
            return await send_error_and_close(self, 4001, "인증 실패: 유효하지 않은 사용자")

        try:
            manager = await get_manager_by_user(user)
            if not manager:
                logger.error(f"CallStaffConsumer: Connection rejected. Manager not found for user {user.id}.")
                return await send_error_and_close(self, 4003, "Manager 정보를 찾을 수 없습니다.")

            if not hasattr(manager, 'booth') or manager.booth is None:
                logger.error(f"CallStaffConsumer: Connection rejected. Manager {manager.id} has no associated booth.")
                return await send_error_and_close(self, 4004, "Manager에 연결된 부스 정보가 없습니다.")

            self.room_group_name = f"booth_{manager.booth.id}_staff_calls"
            await self.accept()
            logger.info(f"CallStaffConsumer: Connection accepted for booth {manager.booth.id}.")
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            logger.info(f"CallStaffConsumer: User {user.id} added to channel group '{self.room_group_name}'.")

        except Exception as e:
            logger.error(f"CallStaffConsumer: Unexpected error during connection for user {user.id}: {e}", exc_info=True)
            try:
                await self.accept()
                await self.send(text_data=json.dumps({
                    "type": "ERROR",
                    "code": 5000,
                    "message": f"서버 연결 중 예기치 않은 오류 발생: {e.__class__.__name__}"
                }))
                await self.close(code=5000)
                logger.warning(f"CallStaffConsumer: Attempted to send error and close after an unexpected exception.")
            except Exception as close_e:
                logger.critical(f"CallStaffConsumer: Failed to send error or close connection after critical exception: {close_e}", exc_info=True)


    async def disconnect(self, close_code):
        logger.info(f"CallStaffConsumer: Disconnection initiated with code {close_code}.")
        if hasattr(self, 'room_group_name') and self.channel_layer:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            logger.info(f"CallStaffConsumer: User removed from channel group '{self.room_group_name}'.")
        else:
            logger.warning("CallStaffConsumer: room_group_name or channel_layer not defined during disconnect. Clean-up skipped.")

    async def staff_call(self, event):
        logger.debug(f"CallStaffConsumer: Handling 'staff_call' event for sending: {event.get('tableNumber')}")
        await self.send(text_data=json.dumps({
            "type": "CALL_STAFF",
            "tableNumber": event["tableNumber"],
            "boothId": event.get("boothId"),
            "message": f"{event['tableNumber']}번 테이블에서 직원을 호출했습니다!"
        }))


# 테이블 상태 대시보드 웹소켓
class TableStatusConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("TableStatusConsumer: Connection attempt started.")
        user = self.scope.get("user")
        
        if not user or not user.is_authenticated:
            logger.warning("TableStatusConsumer: Connection rejected. User not authenticated or invalid.")
            return await send_error_and_close(self, 4001, "인증 실패: 유효하지 않은 사용자")

        try:
            manager = await get_manager_by_user(user)
            if not manager:
                logger.error(f"TableStatusConsumer: Connection rejected. Manager not found for user {user.id}.")
                return await send_error_and_close(self, 4003, "Manager 정보를 찾을 수 없습니다.")

            if not hasattr(manager, 'booth') or manager.booth is None:
                logger.error(f"TableStatusConsumer: Connection rejected. Manager {manager.id} has no associated booth.")
                return await send_error_and_close(self, 4004, "Manager에 연결된 부스 정보가 없습니다.")

            self.room_group_name = f"booth_{manager.booth.id}_tables"
            await self.accept()
            logger.info(f"TableStatusConsumer: Connection accepted for booth {manager.booth.id}.")
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            logger.info(f"TableStatusConsumer: User {user.id} added to channel group '{self.room_group_name}'.")

            table_statuses = await get_table_statuses(user)
            logger.debug(f"TableStatusConsumer: Sending initial table statuses ({len(table_statuses)} tables).")
            await self.send(text_data=json.dumps({
                "type": "TABLE_STATUS",
                "data": table_statuses
            }))

        except Exception as e:
            logger.error(f"TableStatusConsumer: Unexpected error during connection for user {user.id}: {e}", exc_info=True)
            try:
                await self.accept()
                await self.send(text_data=json.dumps({
                    "type": "ERROR",
                    "code": 5000,
                    "message": f"서버 연결 중 예기치 않은 오류 발생: {e.__class__.__name__}"
                }))
                await self.close(code=5000)
                logger.warning(f"TableStatusConsumer: Attempted to send error and close after an unexpected exception.")
            except Exception as close_e:
                logger.critical(f"TableStatusConsumer: Failed to send error or close connection after critical exception: {close_e}", exc_info=True)


    async def disconnect(self, close_code):
        logger.info(f"TableStatusConsumer: Disconnection initiated with code {close_code}.")
        if hasattr(self, 'room_group_name') and self.channel_layer:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            logger.info(f"TableStatusConsumer: User removed from channel group '{self.room_group_name}'.")
        else:
            logger.warning("TableStatusConsumer: room_group_name or channel_layer not defined during disconnect. Clean-up skipped.")

    async def receive(self, text_data):
        logger.debug(f"TableStatusConsumer: Received raw data: {text_data}")
        try:
            data = json.loads(text_data)
            if data.get("type") == "REFRESH":
                user = self.scope.get("user")
                if not user or not user.is_authenticated:
                    logger.warning("TableStatusConsumer: REFRESH request from unauthenticated user. Rejecting.")
                    await self.send(text_data=json.dumps({
                        "type": "ERROR",
                        "code": 4001,
                        "message": "인증 실패: 유효하지 않은 사용자"
                    }))
                    return

                table_statuses = await get_table_statuses(user)
                logger.debug(f"TableStatusConsumer: Sending refreshed table statuses ({len(table_statuses)} tables).")
                await self.send(text_data=json.dumps({
                    "type": "TABLE_STATUS",
                    "data": table_statuses
                }))
            else:
                logger.warning(f"TableStatusConsumer: Unknown message type received: '{data.get('type') or 'N/A'}'")
        except json.JSONDecodeError:
            logger.error(f"TableStatusConsumer: Failed to decode JSON from received data: '{text_data}'", exc_info=True)
        except Exception as e:
            logger.error(f"TableStatusConsumer: Error during receive processing: {e}", exc_info=True)
