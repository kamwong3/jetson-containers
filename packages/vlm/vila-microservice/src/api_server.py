# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from uuid import uuid4
from time import time
import re
import logging

from mmj_utils.api_server import APIServer, APIMessage, Response
from mmj_utils.api_schemas import ChatMessages, StreamAdd

from pydantic import BaseModel, Field, conlist, constr
from typing import Optional, Dict, Any
from fastapi import HTTPException

import os
from config import load_config


class Alerts(BaseModel):
    alerts: conlist(item_type=constr(max_length=100), min_length=0, max_length=10)
    id: Optional[constr(max_length=100)] = ""

class Query(BaseModel):
    query: str = Field(..., max_length=20, min_length=0)

class VLMServer(APIServer):
    def __init__(self, cmd_q, resp_d, port=5000, clean_up_time=180, max_alerts=10):
        super().__init__(cmd_q, resp_d, port=port, clean_up_time=clean_up_time)

        self.max_alerts = max_alerts

        self.app.post("/api/v1/alerts")(self.alerts)
        self.app.post("/api/v1/chat/completions")(self.chat_completion)
        self.app.get("/api/v1/models")(self.get_model_name)

    def get_model_name(self):
        """
        Returns the current model name.
        """
        #Load config
        chat_server_config_path = os.environ["CHAT_SERVER_CONFIG_PATH"]
        chat_server_config = load_config(chat_server_config_path, "chat_server")

        model_name = chat_server_config.model
        current_timestamp = int(time())  # Substitute by current timestamp

        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": current_timestamp,
                    "owned_by": "system"
                }
            ]
        }

    def alerts(self, body:Alerts):
        user_alerts = dict()

        #parse input alerts
        for x, alert in enumerate(body.alerts):
            if x >= self.max_alerts:
                break

            alert = alert.strip()

            if alert != "":
                user_alerts[f"r{x}"] = alert

        message_data = user_alerts
        queue_message = APIMessage(type="alert", data=message_data, id=str(uuid4()), time=time())

        self.cmd_q.put(queue_message)
        self.cmd_tracker[queue_message.id] = queue_message.time

        response = self.get_command_response(queue_message.id)
        if response:
            return Response(detail=response)
        else:
            raise HTTPException(status_code=500, detail="Server timed out processing the request")

    def chat_completion(self, body: ChatMessages):
        # Check if there's a v4l2 URL in the request
        v4l2_device = None
        stream_id = None

        # Look for v4l2 URLs in the messages
        for message in body.messages:
            if message.role == "user" and isinstance(message.content, list):
                for content in message.content:
                    if content.type == "image_url" and hasattr(content, "image_url"):
                        url = content.image_url.url
                        # Check if it's a v4l2 URL
                        v4l2_match = re.match(r'v4l2:///dev/video(\d+)', url)
                        if v4l2_match:
                            v4l2_device = f"/dev/video{v4l2_match.group(1)}"
                            logging.info(f"Found v4l2 device: {v4l2_device}")

        # If v4l2 device found, check if there's already a stream connected with this device
        if v4l2_device:
            # Check if there's already a stream connected
            existing_streams = self.stream_list()
            stream_already_connected = False

            for stream in existing_streams:
                if stream.liveStreamUrl == v4l2_device:
                    # Stream with this device is already connected, use it
                    stream_id = stream.id
                    stream_already_connected = True
                    logging.info(f"Using existing v4l2 stream with ID: {stream_id}")
                    break

            # If no stream is connected with this device, add a new one
            if not stream_already_connected:
                # Create a StreamAdd object
                stream_add_body = StreamAdd(liveStreamUrl=v4l2_device, description="v4l2 camera")

                try:
                    # Call the stream_add function directly
                    stream_result = self.stream_add(stream_add_body)
                    stream_id = stream_result["id"]
                    logging.info(f"Added v4l2 stream with ID: {stream_id}")
                except Exception as e:
                    logging.error(f"Failed to add v4l2 stream: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"Failed to add v4l2 stream: {str(e)}")

        # Process the chat completion request
        queue_message = APIMessage(type="query", data=body, id=str(uuid4()), time=time())
        self.cmd_q.put(queue_message)
        self.cmd_tracker[queue_message.id] = queue_message.time

        response = self.get_command_response(queue_message.id)

        # Keep the stream connected after processing
        # (removed the stream removal code to maintain the connection)

        if response:
            return response
        else:
            raise HTTPException(status_code=500, detail="Server timed out processing the request")
