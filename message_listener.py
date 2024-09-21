import asyncio
import websockets
import json
from logger import setup_logger
from heartbeat_handler import HeartbeatHandler
from dify_client import DifyClient  # 导入异步 DifyClient
from plugin_manager import PluginManager  # 导入插件管理器
from config_manager import ConfigManager  # 导入配置管理器

class MessageListener:
    """WebSocket 服务器，等待 OneBot 的连接"""

    def __init__(self, host, port, path, logger=None):
        self.host = host
        self.port = port
        self.path = path
        self.logger = logger or setup_logger('MessageListener')
        self.heartbeat_handler = None
        self.plugin_manager = PluginManager()  # 初始化插件管理器
        self.config_manager = ConfigManager()  # 初始化配置管理器

    async def handler(self, websocket, path):
        """处理收到的消息"""
        if path == self.path:
            self.logger.info(f'OneBot 已连接：{path}')
            self.plugin_manager.load_plugins()  # 加载启用的插件
            try:
                async for message in websocket:
                    msg_data = json.loads(message)
                    if msg_data.get('post_type') == 'meta_event' and msg_data.get('meta_event_type') == 'heartbeat':
                        self.heartbeat_handler.add_heartbeat(msg_data)
                    elif msg_data.get('post_type') == 'message':
                        self.logger.debug(f'收到原始QQ消息：{message}')  # 记录非心跳消息
                        if msg_data.get('message_type') == 'private':
                            await self.process_private_message(websocket, msg_data)
                        elif msg_data.get('message_type') == 'group':
                            await self.process_group_message(websocket, msg_data)
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.info('连接已关闭')
        else:
            self.logger.warning(f'收到未知路径的连接：{path}')
            await websocket.close()

    async def process_private_message(self, websocket, msg_data):
        """处理 QQ 用户发送的私聊消息"""
        user_id = msg_data['sender']['user_id']
        message_text = msg_data.get('raw_message', '')
        self.logger.info(f'收到来自 {user_id} 的私聊消息：{message_text}')

        # 优先交给插件管理器处理消息
        plugin_result = self.plugin_manager.handle_message(msg_data)

        if plugin_result["handled"]:
            if plugin_result["reply"] is not None:
                reply = {
                    "action": "send_private_msg",
                    "params": {
                        "user_id": user_id,
                        "message": plugin_result["reply"]
                    },
                    "echo": "send_private_msg"
                }
                await websocket.send(json.dumps(reply))
                self.logger.debug(f'已向 {user_id} 发送插件回复')
            else:
                self.logger.debug(f'插件处理后取消回复')
            return  # 插件已处理消息，跳过后续处理

        # 如果插件没有处理，使用 DifyClient 发送请求 (异步调用)
        response = await self.dify_client.send_request(message_text, user_id, is_group=False)
        answer = self.dify_receiver.process_response(response)

        if answer:
            reply = {
                "action": "send_private_msg",
                "params": {
                    "user_id": user_id,
                    "message": answer
                },
                "echo": "send_private_msg"
            }
            await websocket.send(json.dumps(reply))
            self.logger.debug(f'已向 {user_id} 发送回复')
        else:
            self.logger.error('未能从 Dify 获取有效响应')

    async def process_group_message(self, websocket, msg_data):
        """处理 QQ 群聊消息"""
        group_id = msg_data['group_id']
        user_id = msg_data['sender']['user_id']
        message_text = msg_data.get('raw_message', '')
        self.logger.info(f'收到来自群 {group_id} 用户 {user_id} 的群聊消息：{message_text}')

        # 从配置文件中检查是否允许回复该群聊消息
        group_setting = self.config_manager.get_group_chat_setting(group_id)
        if not group_setting.get('enabled', True):
            self.logger.debug(f'群 {group_id} 已被配置为不回复消息')
            return

        # 优先交给插件管理器处理消息
        plugin_result = self.plugin_manager.handle_message(msg_data)

        if plugin_result["handled"]:
            if plugin_result["reply"] is not None:
                reply = {
                    "action": "send_group_msg",
                    "params": {
                        "group_id": group_id,
                        "message": plugin_result["reply"]
                    },
                    "echo": "send_group_msg"
                }
                await websocket.send(json.dumps(reply))
                self.logger.debug(f'已向群 {group_id} 发送插件回复')
            else:
                self.logger.debug(f'插件处理后取消回复')
            return  # 插件已处理消息，跳过后续处理

        # 如果插件没有处理，使用 DifyClient 发送请求 (异步调用)
        response = await self.dify_client.send_request(message_text, group_id, is_group=True)
        answer = self.dify_receiver.process_response(response)

        if answer:
            reply = {
                "action": "send_group_msg",
                "params": {
                    "group_id": group_id,
                    "message": answer
                },
                "echo": "send_group_msg"
            }
            await websocket.send(json.dumps(reply))
            self.logger.debug(f'已向群 {group_id} 发送回复')
        else:
            self.logger.error('未能从 Dify 获取有效响应')


    async def start(self, dify_client, dify_receiver):
        """启动 WebSocket 服务器"""
        self.dify_client = dify_client
        self.dify_receiver = dify_receiver
        self.heartbeat_handler = HeartbeatHandler(self.logger, interval=300)

        async def ws_handler(websocket, path):
            await self.handler(websocket, path)

        server = await websockets.serve(ws_handler, self.host, self.port)
        self.logger.info(f'WebSocket 服务器已启动，监听 {self.host}:{self.port}{self.path}')
        await server.wait_closed()
