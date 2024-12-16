# chatbot/schedule_api.py

import requests
import os
import logging
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class ScheduleAPI:
    BASE_URL = os.getenv("API_BASE_URL", "http://localhost:5000")  # Default to localhost if not set

    def post_to_create_event(self, conversation_id: str, interviewee_number: str) -> Optional[Dict]:
        """
        Initiates the event creation process for a specific interviewee.

        Args:
            conversation_id (str): The ID of the conversation.
            interviewee_number (str): The phone number of the interviewee.

        Returns:
            Optional[Dict]: The API response containing 'event_id' and 'event', or None if failed.
        """
        try:
            url = f"{self.BASE_URL}/api/create_event/{conversation_id}"
            data = {'interviewee_number': interviewee_number}
            headers = {
                "x-api-key": os.getenv("API_KEY"),
                "Content-Type": "application/json"
            }
            response = requests.post(url, json=data, headers=headers)
            response.raise_for_status()
            api_response = response.json()

            # Check if 'event_id' is present at top level
            if 'event_id' in api_response:
                logger.info(f"Event ID {api_response['event_id']} retrieved successfully.")
            else:
                logger.warning(f"'event_id' not found in API response: {api_response}")

            logger.info(f"Event created successfully for conversation {conversation_id} and interviewee {interviewee_number} with response: {api_response}")
            return api_response  # Now contains 'event_id' directly
        except requests.RequestException as e:
            logger.error(f"Error creating event for conversation {conversation_id} and interviewee {interviewee_number}: {e}")
            return None
