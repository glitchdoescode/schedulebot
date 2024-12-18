# calendar_module/calendar_service.py

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from calendar_module.auth import load_credentials
from datetime import datetime
import pytz
import logging
import os
from dotenv import load_dotenv
from pymongo import MongoClient
import time
from typing import Tuple, Optional, Dict, Any

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB setup
MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGODB_DB_NAME")

client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
conversations_collection = db.conversations

class CalendarService:
    def create_event(self, conversation_id: str, interviewee_number: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Creates a Google Calendar event for a specific interviewee.

        Args:
            conversation_id (str): The ID of the conversation
            interviewee_number (str): The phone number of the interviewee

        Returns:
            Tuple[Optional[Dict[str, Any]], Optional[str]]: Tuple containing the event details and an error message if any
        """
        try:
            # Validate inputs
            if not conversation_id or not interviewee_number:
                logger.error("Missing required parameters")
                return None, "Missing required parameters"

            # Retrieve conversation from MongoDB
            conversation = conversations_collection.find_one({'conversation_id': conversation_id})
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found.")
                return None, f"Conversation {conversation_id} not found"

            interviewer = conversation.get('interviewer')
            if not interviewer:
                logger.error(f"Interviewer not found in conversation {conversation_id}")
                return None, "Interviewer information missing"

            # Find the specific interviewee
            interviewee = next((ie for ie in conversation.get('interviewees', []) 
                              if ie['number'] == interviewee_number), None)
            if not interviewee:
                logger.error(f"Interviewee {interviewee_number} not found")
                return None, f"Interviewee {interviewee_number} not found"

            scheduled_slot = interviewee.get('scheduled_slot')
            if not scheduled_slot:
                logger.error(f"No scheduled slot found for interviewee {interviewee_number}")
                return None, "No scheduled slot found"

            # Validate required fields
            required_fields = ['start_time', 'end_time']
            if not all(field in scheduled_slot for field in required_fields):
                logger.error("Missing required scheduling information")
                return None, "Invalid scheduling information"

            # Convert times to datetime objects
            try:
                meeting_start = datetime.fromisoformat(scheduled_slot['start_time'])
                meeting_end = datetime.fromisoformat(scheduled_slot['end_time'])
            except ValueError as e:
                logger.error(f"Invalid datetime format: {e}")
                return None, "Invalid datetime format"

            # Get timezone, defaulting to UTC if not specified
            time_zone = interviewer.get('timezone', 'UTC')
            try:
                tz = pytz.timezone(time_zone)
                start_datetime_local = meeting_start.astimezone(tz)
                end_datetime_local = meeting_end.astimezone(tz)
            except pytz.exceptions.UnknownTimeZoneError:
                logger.error(f"Unknown timezone: {time_zone}")
                # Fall back to UTC
                time_zone = 'UTC'
                tz = pytz.UTC
                start_datetime_local = meeting_start.astimezone(tz)
                end_datetime_local = meeting_end.astimezone(tz)
                logger.info(f"Falling back to UTC timezone for conversation {conversation_id}")

            # Prepare event details
            event = {
                'summary': f'Interview with {interviewee["name"]}',
                'description': 'Interview scheduled via the scheduling assistant.',
                'start': {
                    'dateTime': start_datetime_local.isoformat(),
                    'timeZone': time_zone
                },
                'end': {
                    'dateTime': end_datetime_local.isoformat(),
                    'timeZone': time_zone
                },
                'attendees': [
                    {'email': interviewer['email']},
                    {'email': interviewee['email']}
                ],
                'conferenceData': {
                    'createRequest': {
                        'requestId': f"meet-{conversation_id}-{interviewee_number}-{int(datetime.now().timestamp())}",
                        'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                    }
                }
            }

            # Create calendar event
            credentials = load_credentials()
            if not credentials:
                return None, "Failed to load credentials"

            service = build('calendar', 'v3', credentials=credentials)
            event_result = service.events().insert(
                calendarId='primary',
                body=event,
                conferenceDataVersion=1,
                sendUpdates='all'
            ).execute()

            if not event_result:
                logger.error("Failed to create calendar event")
                return None, "Failed to create calendar event"

            # Update MongoDB with event ID
            update_result = conversations_collection.update_one(
                {
                    'conversation_id': conversation_id,
                    'interviewees.number': interviewee_number
                },
                {
                    '$set': {
                        'interviewees.$.event_id': event_result.get('id'),
                        'interviewees.$.calendar_link': event_result.get('htmlLink')
                    }
                }
            )

            if update_result.modified_count == 0:
                logger.warning("Failed to update conversation with event ID")

            logger.info(f"Event created successfully: {event_result.get('htmlLink')}")
            return {
                'event_id': event_result.get('id'),
                'event': event_result
            }, None

        except HttpError as e:
            error_message = f"Google Calendar API error: {str(e)}"
            logger.error(error_message)
            return None, error_message
        except Exception as e:
            error_message = f"Unexpected error creating calendar event: {str(e)}"
            logger.error(error_message)
            return None, error_message

    def delete_event(self, event_id: str, max_retries: int = 3, initial_retry_delay: float = 1.0) -> bool:
        """
        Deletes an event from Google Calendar with retry logic.

        Args:
            event_id (str): The ID of the event to delete.
            max_retries (int): Maximum number of retries.
            initial_retry_delay (float): Initial delay between retries in seconds.

        Returns:
            bool: True if deletion was successful, False otherwise.
        """
        retry_count = 0
        current_delay = initial_retry_delay

        while retry_count < max_retries:
            try:
                logger.debug(f"Deleting event with ID: {event_id}")
                service = build('calendar', 'v3', credentials=load_credentials())
                service.events().delete(calendarId='primary', eventId=event_id).execute()
                logger.info(f"Event with ID {event_id} deleted successfully.")
                return True
            except HttpError as error:
                retry_count += 1
                logger.warning(f"Attempt {retry_count} failed to delete event {event_id}: {error}")
                if retry_count >= max_retries:
                    logger.error(f"Failed to delete event {event_id} after {max_retries} attempts.")
                    return False
                time.sleep(current_delay)
                current_delay = min(current_delay * 2, 30)  # Exponential backoff
            except Exception as e:
                retry_count += 1
                logger.warning(f"Unexpected error on attempt {retry_count} deleting event {event_id}: {e}")
                if retry_count >= max_retries:
                    logger.error(f"Failed to delete event {event_id} after {max_retries} attempts due to unexpected error.")
                    return False
                time.sleep(current_delay)
                current_delay = min(current_delay * 2, 30)  # Exponential backoff

    def update_event(self, conversation_id: str, event_id: str, new_start_time: str, new_end_time: str) -> bool:
        """
        Update the event's start and end time on the calendar.
        Returns True if successful, False otherwise.
        """
        try:
            credentials = load_credentials()
            if not credentials:
                logger.error("Failed to load credentials for update_event")
                return False

            service = build('calendar', 'v3', credentials=credentials)
            event_body = {
                'start': {'dateTime': new_start_time},
                'end': {'dateTime': new_end_time}
            }

            updated_event = service.events().patch(
                calendarId='primary',
                eventId=event_id,
                body=event_body,
                sendUpdates='all'
            ).execute()

            if updated_event:
                logger.info(f"Event {event_id} updated successfully for conversation {conversation_id}.")
                return True
            else:
                logger.error(f"Failed to update event {event_id}. No event returned.")
                return False
        except HttpError as e:
            logger.error(f"Google Calendar API error during update_event: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating calendar event {event_id}: {str(e)}")
            return False
