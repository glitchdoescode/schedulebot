# chatbot/message_handler.py

import logging
import random
import time
import os
from datetime import datetime, timedelta
import pytz
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from chatbot.constants import ConversationState
from chatbot.utils import (
    extract_slots_and_timezone,
    normalize_number,
    extract_timezone_from_number,
    get_localized_current_time
)
from dotenv import load_dotenv
from .llm.llmmodel import LLMModel
import traceback
from typing import Optional

load_dotenv()

logger = logging.getLogger(__name__)

# NEW: A configurable constant for how many times we may ask the interviewer for more slots
MAX_SLOT_REQUESTS = 2  # You can make this dynamic or adjustable as needed.

class MessageHandler:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.llm_model = LLMModel()

    def send_message(self, to_number: str, message: str, max_retries: int = 3, initial_retry_delay: float = 1.0) -> bool:
        """
        Sends a WhatsApp message using Twilio. Includes exponential backoff with jitter to avoid infinite retries.
        - If sending fails after max_retries, create an attention flag (if possible).
        """
        # Ensure we send via WhatsApp
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'

        twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        twilio_whatsapp_number = os.getenv('TWILIO_WHATSAPP_NUMBER')

        if not all([twilio_account_sid, twilio_auth_token, twilio_whatsapp_number]):
            logger.error("Missing Twilio credentials. Check environment variables.")
            # We don't have a conversation_id here to tie to a specific conversation
            # but you can still create a general attention flag if desired.
            return False

        client = Client(twilio_account_sid, twilio_auth_token)
        retry_count = 0
        current_delay = initial_retry_delay
        last_exception = None

        # Attempt to send the message up to max_retries times
        while retry_count <= max_retries:
            try:
                sent_message = client.messages.create(
                    body=message,
                    from_=twilio_whatsapp_number,
                    to=to_number
                )
                logger.info(f"Message sent successfully to {to_number}: SID {sent_message.sid}")
                return True
            except TwilioRestException as e:
                retry_count += 1
                last_exception = e
                logger.warning(
                    f"Twilio error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: Error {e.code} - {e.msg}"
                )

                # Specific error handling for Twilio
                if e.code in [20003, 20426]:
                    logger.error("Authentication failed. Check Twilio credentials.")
                    return False
                elif e.code in [21211, 21614]:
                    logger.error(f"Invalid phone number: {to_number}")
                    return False
                elif e.code == 21617:
                    logger.error("Message exceeds maximum length.")
                    return False
            except Exception as e:
                retry_count += 1
                last_exception = e
                logger.warning(
                    f"Unexpected error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: {str(e)}"
                )

            if retry_count > max_retries:
                logger.error(
                    f"Failed to send message to {to_number} after {max_retries} attempts. "
                    f"Last error: {str(last_exception)}"
                )
                # --- NEW: Create an attention flag for repeated Twilio errors ---
                # We don't have a conversation ID in this context,
                # but if you'd like to track the phone number at least:
                self._create_general_attention_flag(
                    title="Twilio Send Error",
                    description=(
                        f"Failed to send message to {to_number} after {max_retries} attempts. "
                        f"Error details: {str(last_exception)}"
                    )
                )
                return False

            # Exponential backoff with jitter
            jitter = random.uniform(0, 0.1) * current_delay
            sleep_time = current_delay + jitter
            logger.debug(f"Retrying in {sleep_time:.2f} seconds...")
            time.sleep(sleep_time)
            current_delay = min(current_delay * 2, 30)

        return False

    def generate_response(
        self,
        participant: dict,
        other_participant: Optional[dict],
        user_message: str,
        system_message: str,
        conversation_state: Optional[str] = None,
        message_type: str = 'generate_message'
    ) -> str:
        """
        Generates a response using an LLM model. 
        This function packages up the participant's context and uses either
        generate_message or answer_query from the LLMModel depending on message_type.
        """
        conversation_state = conversation_state or participant.get('state')
        conversation_history = " ".join(participant.get('conversation_history', []))
        if other_participant:
            _ = " ".join(other_participant.get('conversation_history', []))

        params = {
            'participant_name': participant['name'],
            'participant_number': participant['number'],
            'participant_email': participant.get('email', ''),
            'participant_role': participant.get('role', ''),
            'superior_flag': participant.get('superior_flag', False),
            'meeting_duration': participant.get('meeting_duration', 60),
            'role_to_contact_name': participant.get('role_to_contact_name', ''),
            'role_to_contact_number': participant.get('role_to_contact_number', ''),
            'role_to_contact_email': participant.get('role_to_contact_email', ''),
            'company_details': participant.get('company_details', ''),
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message,
            'system_message': system_message
        }

        try:
            # Call LLM for either a regular message or a question-answer style response
            if message_type == 'generate_message':
                response = self.llm_model.generate_message(**params)
            elif message_type == 'answer_query':
                response = self.llm_model.answer_query(**params)
            else:
                raise ValueError(f"Unknown message_type: {message_type}")
            return response
        except Exception as e:
            logger.error(f"Error generating response: {str(e)}")
            logger.error(traceback.format_exc())
            return "The AI assistant encountered an error while processing the request."

    def receive_message(self, from_number: str, message: str):
        """
        Main entry point for handling an incoming message from a participant. 
        Added a check to stop processing if the conversation is already completed,
        avoiding repeated messages once scheduling is done.
        """
        # Identify which conversation and participant this message is about
        conversation_id, participant, interviewer_number = self.find_conversation_and_participant(from_number, message)
        if not conversation_id or not participant:
            logger.warning(f"No active conversation found for number: {from_number}")
            # Optional: create a general attention flag if you'd like to track missing conversation cases
            return

        # Pull the conversation from storage
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.warning(f"Conversation {conversation_id} not found or previously removed.")
            # If you want to track it:
            self._create_conversation_attention_flag(
                conversation_id,
                title="Missing Conversation",
                description=f"Conversation {conversation_id} not found in DB or removed unexpectedly."
            )
            return
        
        # --- NEW: Check if the conversation is already completed ---
        if conversation.get('status') == 'completed':
            logger.info(f"Conversation {conversation_id} is already completed; ignoring further messages.")
            return
        # --- END NEW CHECK ---

        now_utc = datetime.now(pytz.UTC)
        self.scheduler.mongodb_handler.update_conversation(
            conversation_id,
            {f'last_response_times.{participant["number"]}': now_utc.isoformat()}
        )

        # Log the conversation history
        self.scheduler.log_conversation_history(conversation_id)
        self.scheduler.log_conversation(conversation_id, participant['number'], "user", message, "Participant")

        # Detect the participant's intent
        intent = self.llm_model.detect_intent(
            participant_name=participant['name'],
            participant_role=participant.get('role', ''),
            meeting_duration=participant.get('meeting_duration', 60),
            role_to_contact=participant.get('role_to_contact_name', ''),
            conversation_history=" ".join(participant.get('conversation_history', [])),
            conversation_state=participant.get('state', ''),
            user_message=message
        )
        logger.info(f"Detected intent: {intent}")

        # Branch logic based on intent
        if "CANCELLATION_REQUESTED" in intent:
            if participant.get('role') == 'interviewer':
                self.handle_cancellation_request_interviewer(conversation_id, participant, message)
            else:
                self.handle_cancellation_request_interviewee(conversation_id, participant, message)

        elif "QUERY" in intent:
            self.handle_query(conversation_id, participant, message)

        elif "RESCHEDULE_REQUESTED" in intent:
            if participant.get('role') == 'interviewer':
                self.handle_reschedule_request_interviewer(conversation_id, participant, message)
            else:
                self.handle_reschedule_request_interviewee(conversation_id, participant, message)

        else:
            # Default handling: message from interviewer or interviewee
            if participant.get('role') == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def find_conversation_and_participant(self, from_number: str, message: str):
        """
        Look up the conversation and participant based on the phone number. 
        Prefers active conversations, then queued, ignoring completed ones.
        """
        from_number_norm = normalize_number(from_number)
        conversations = self.scheduler.mongodb_handler.find_conversations_by_number(from_number_norm)

        if not conversations:
            return None, None, None

        if len(conversations) == 1:
            conversation = conversations[0]
            participant = (
                conversation['interviewer'] 
                if conversation['interviewer']['number'] == from_number_norm
                else next((ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            )
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        active_conversations = [c for c in conversations if c['status'] == 'active']
        if active_conversations:
            conversation = sorted(active_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (
                conversation['interviewer'] 
                if conversation['interviewer']['number'] == from_number_norm
                else next((ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            )
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        queued_conversations = [c for c in conversations if c['status'] == 'queued']
        if queued_conversations:
            conversation = sorted(queued_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (
                conversation['interviewer'] 
                if conversation['interviewer']['number'] == from_number_norm
                else next((ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            )
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        return None, None, None

    def handle_message_from_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        """
        Handles any message coming from the interviewer, which could be initial 
        slot gathering or additional slots upon request, or confirmations.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            # Create attention flag if conversation missing
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_message_from_interviewer",
                description=f"No conversation found for {conversation_id}"
            )
            return

        # If the interviewer is in AWAITING_SLOT_CONFIRMATION, they had just provided some slots
        if interviewer.get('state') == ConversationState.AWAITING_SLOT_CONFIRMATION.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewer['name'],
                participant_role=interviewer.get('role', ''),
                meeting_duration=interviewer.get('meeting_duration', 60),
                conversation_history=" ".join(interviewer.get('conversation_history', [])),
                conversation_state=interviewer.get('state', ''),
                user_message=message
            )

            if confirmation_response.get('confirmed'):
                # The interviewer confirmed the slots
                temp_slots = interviewer.get('temp_slots')
                if not temp_slots:
                    # Safety check: if something went wrong and we have no stored slots
                    tz_str = interviewer.get('timezone', 'UTC')
                    local_now = get_localized_current_time(tz_str)
                    system_message = (
                        "Instruct the AI assistant to inform the interviewer that there was an error "
                        "with the previously identified time slots and to please provide them again.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    # Create an attention flag for missing temp_slots if you want:
                    self._create_conversation_attention_flag(
                        conversation_id,
                        title="Temp Slots Missing",
                        description="Interviewer confirmed slots but no temp_slots found."
                    )
                    return

                available_slots = conversation.get('available_slots', [])
                new_slots = temp_slots.get('time_slots', [])

                # Filter out duplicates
                existing_slot_keys = {self._create_slot_key(slot) for slot in available_slots}
                filtered_new_slots = [
                    slot for slot in new_slots
                    if self._create_slot_key(slot) not in existing_slot_keys
                ]
                available_slots.extend(filtered_new_slots)

                conversation['available_slots'] = available_slots
                interviewer['temp_slots'] = None
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                # Update the conversation with the new slots
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': available_slots,
                    'interviewer': interviewer
                })

                # Acknowledge the interviewer
                tz_str = interviewer.get('timezone', 'UTC')
                local_now = get_localized_current_time(tz_str)
                system_message = (
                    "Instruct the AI assistant to confirm to the interviewer that their slots have been received "
                    "and the assistant will proceed with scheduling the interviews using these new slots.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                # Attempt scheduling for any interviewees who had no slots or were awaiting
                self.initiate_scheduling_for_no_slots_available(conversation_id)
                self.initiate_scheduling_for_awaiting_availability(conversation_id)

            else:
                # Interviewer refused the slots or typed something else
                extracted_data = extract_slots_and_timezone(
                    message,
                    interviewer['number'],
                    interviewer.get('conversation_history', []),
                    interviewer.get('meeting_duration', 60)
                )
                tz_str = interviewer.get('timezone', 'UTC')
                local_now = get_localized_current_time(tz_str)

                if extracted_data and 'time_slots' in extracted_data:
                    # The interviewer provided new slots inline after refusing
                    interviewer['temp_slots'] = extracted_data
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    formatted_slots = []
                    for slot in extracted_data.get("time_slots", []):
                        start_time = datetime.fromisoformat(slot['start_time'])
                        tz = extracted_data.get('timezone', 'UTC')
                        slot_str = start_time.astimezone(pytz.timezone(tz)).strftime('%A, %B %d, %Y at %I:%M %p %Z')
                        formatted_slots.append(f"- {slot_str}")
                    slots_text = "\n".join(formatted_slots)

                    system_message = (
                        "Instruct the AI assistant to inform the interviewer that the following new slots "
                        f"have been parsed:\n\n{slots_text}\n\n"
                        "Ask the interviewer to reply with 'yes' to confirm these slots or 'no' to change them.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    # No valid new slots recognized
                    interviewer['temp_slots'] = None
                    interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    system_message = (
                        "Instruct the AI assistant to request the interviewer to share availability again in a clear format.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

        elif interviewer.get('state') == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            # The system specifically requested more slots from the interviewer
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
            )
            tz_str = interviewer.get('timezone', 'UTC')
            local_now = get_localized_current_time(tz_str)

            if extracted_data and 'time_slots' in extracted_data:
                available_slots = conversation.get('available_slots', [])
                new_slots = extracted_data.get('time_slots', [])
                existing_keys = {self._create_slot_key(slot) for slot in available_slots}
                filtered_new_slots = [
                    slot for slot in new_slots
                    if self._create_slot_key(slot) not in existing_keys
                ]
                available_slots.extend(filtered_new_slots)

                conversation['more_slots_requests'] = conversation.get('more_slots_requests', 0)
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': available_slots,
                    'interviewer': interviewer
                })

                # Notify interviewer that the new slots have been received
                system_message = (
                    "Instruct the AI assistant to confirm to the interviewer that the new slots have been received. "
                    "Then the assistant should attempt to schedule any remaining interviewees.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                # Make any unscheduled interviewees AWAITING_AVAILABILITY
                unscheduled = [
                    ie for ie in conversation['interviewees']
                    if ie['state'] in [ConversationState.NO_SLOTS_AVAILABLE.value,
                                       ConversationState.AWAITING_AVAILABILITY.value]
                ]
                for ie in unscheduled:
                    ie['state'] = ConversationState.AWAITING_AVAILABILITY.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                # Start scheduling again for the first unscheduled interviewee, if any
                if unscheduled:
                    self.process_scheduling_for_interviewee(conversation_id, unscheduled[0]['number'])
                else:
                    # If no unscheduled interviewees are left, check if we can complete the conversation
                    if self.scheduler.is_conversation_complete(conversation):
                        self.complete_conversation(conversation_id)
                    else:
                        # If there's partial state but no pending interviewees, finalize to avoid deadlock
                        self.complete_conversation(conversation_id)

            else:
                # We could not parse any slots from the interviewer's reply
                system_message = (
                    "Instruct the AI assistant to inform the interviewer that no valid time slots were "
                    "detected in their message. Request them to please provide clear availability again.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

        else:
            # Normal scenario: the interviewer shares slots for the first time or is continuing conversation
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
            )
            tz_str = interviewer.get('timezone', 'UTC')
            local_now = get_localized_current_time(tz_str)

            if extracted_data and 'time_slots' in extracted_data:
                # Store as temporary and ask for confirmation
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    tz = extracted_data.get('timezone', 'UTC')
                    local_str = start_time.astimezone(pytz.timezone(tz)).strftime('%A, %B %d, %Y at %I:%M %p %Z')
                    formatted_slots.append(f"- {local_str}")
                slots_text = "\n".join(formatted_slots)

                system_message = (
                    "Instruct the AI assistant to tell the interviewer that the following slots were identified:\n\n"
                    f"{slots_text}\n\n"
                    "Ask the interviewer to reply with 'yes' to confirm these slots or 'no' if they need to provide different slots.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
            else:
                # Could not parse any slots at all
                system_message = (
                    "Instruct the AI assistant to inform the interviewer that their availability could not be understood "
                    "and to please provide it in a clear format.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

    def handle_message_from_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        """
        Handles any message coming from an interviewee. 
        Typically we only expect a confirmation or denial of a proposed slot.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_message_from_interviewee",
                description=f"No conversation found for {conversation_id}"
            )
            return

        if interviewee.get('state') == ConversationState.CONFIRMATION_PENDING.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewee['name'],
                participant_role=interviewee.get('role', ''),
                meeting_duration=interviewee.get('meeting_duration', 60),
                conversation_history=" ".join(interviewee.get('conversation_history', [])),
                conversation_state=interviewee.get('state', ''),
                user_message=message
            )

            if confirmation_response.get('confirmed'):
                self._handle_slot_acceptance(conversation_id, interviewee, conversation)
            else:
                # Pass the raw message so we can detect if they are proposing a different time
                self._handle_slot_denial(conversation_id, interviewee, conversation, message)

    def _handle_slot_acceptance(self, conversation_id: str, interviewee: dict, conversation: dict):
        """
        If the interviewee accepts a slot, remove it from availability, mark them SCHEDULED, 
        and finalize if needed.
        """
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])
        if not interviewee.get('proposed_slot'):
            # Safety check in case there's no slot proposed
            self._create_conversation_attention_flag(
                conversation_id,
                title="Slot Acceptance Error",
                description=f"Interviewee {interviewee['name']} confirmed but no 'proposed_slot' found."
            )
            return

        accepted_slot_key = self._create_slot_key(interviewee['proposed_slot'])

        # Remove from reserved and global availability
        reserved_slots = [
            slot for slot in reserved_slots
            if self._create_slot_key(slot) != accepted_slot_key
        ]
        available_slots = [
            slot for slot in available_slots
            if self._create_slot_key(slot) != accepted_slot_key
        ]

        interviewee['confirmed'] = True
        interviewee['state'] = ConversationState.SCHEDULED.value

        # Update the conversation data
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': available_slots
        })

        # Possibly finalize or move on
        self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def _handle_slot_denial(self, conversation_id: str, interviewee: dict, conversation: dict, message: str):
        """
        If the interviewee denies a slot, remove them from reservation, 
        and either propose another slot or mark them NO_SLOTS_AVAILABLE.
        
        NEW: If the interviewee attempts to propose a new time, we politely reject it
             (per the requirement to only consider the interviewer's times).
        """
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])
        slot_denials = conversation.get('slot_denials', {})

        # Convert list -> set to handle further updates
        for k, val in slot_denials.items():
            if isinstance(val, list):
                slot_denials[k] = set(val)

        denied_slot = interviewee['proposed_slot']
        denied_slot_key = self._create_slot_key(denied_slot) if denied_slot else None

        # Mark the previously offered slot as denied
        interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [denied_slot] if denied_slot else []
        interviewee['proposed_slot'] = None

        # Remove the denied slot from reserved (so it can be offered to others if not globally denied)
        if denied_slot_key:
            reserved_slots = [
                slot for slot in reserved_slots
                if self._create_slot_key(slot) != denied_slot_key
            ]

            if denied_slot_key not in slot_denials:
                slot_denials[denied_slot_key] = set()
            slot_denials[denied_slot_key].add(interviewee['number'])

        # --- NEW: Check if the interviewee is suggesting a new time, which we must ignore ---
        # We can attempt to parse the new time from the user's message. If found, we reject politely.
        proposed_time_extracted = extract_slots_and_timezone(
            message,
            interviewee["number"],
            interviewee.get("conversation_history", []),
            interviewee.get('meeting_duration', 60)
        )
        if proposed_time_extracted and 'time_slots' in proposed_time_extracted and proposed_time_extracted['time_slots']:
            # The interviewee tried to propose their own time. We politely inform them we only use interviewer times.
            tz_str = interviewee.get('timezone', 'UTC')
            local_now = get_localized_current_time(tz_str)
            system_message = (
                "Politely inform the interviewee that only the interviewer's provided time slots are considered. "
                "Ignore their proposed time and proceed to offer the next interviewer-provided slot.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)
        # --- END NEW ---

        # Check if all unscheduled interviewees have denied this slot => remove from global availability
        unscheduled_ies = [
            ie for ie in conversation['interviewees']
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]
        ]
        all_unscheduled_nums = {ie['number'] for ie in unscheduled_ies}

        if denied_slot_key and slot_denials[denied_slot_key].issuperset(all_unscheduled_nums):
            before_count = len(available_slots)
            available_slots = [
                slot for slot in available_slots
                if self._create_slot_key(slot) != denied_slot_key
            ]
            after_count = len(available_slots)
            if after_count < before_count:
                logger.info(
                    f"Slot {denied_slot} removed from available_slots "
                    f"because all unscheduled interviewees denied it."
                )

        conversation['available_slots'] = available_slots
        conversation['slot_denials'] = {
            k: list(v) for k, v in slot_denials.items()
        }

        # Reassign updated interviewee object
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        # Check for any untried slots left for this interviewee
        untried_slots = self._get_untried_slots_for_interviewee(interviewee, available_slots, reserved_slots)
        if untried_slots:
            interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
        else:
            interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
            logger.info(
                f"Interviewee {interviewee['name']} moved to NO_SLOTS_AVAILABLE after denying all offered slots."
            )

        # Update conversation with the new interviewee state
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': conversation['available_slots'],
            'slot_denials': conversation['slot_denials']
        })

        # Continue scheduling attempts for others or finalize
        self.process_remaining_interviewees(conversation_id)

    def process_remaining_interviewees(self, conversation_id: str):
        """
        Iterates over any interviewees who are AWAITING_AVAILABILITY or NO_SLOTS_AVAILABLE 
        to see if we can schedule them or if we need to request more slots from the interviewer. 
        Avoids infinite loops by systematically iterating until no more changes can be made.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in process_remaining_interviewees",
                description=f"No conversation found for {conversation_id}"
            )
            return

        changed_something = True
        while changed_something:
            changed_something = False
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                self._create_conversation_attention_flag(
                    conversation_id,
                    title="No Conversation in loop of process_remaining_interviewees",
                    description="Loop ended due to missing conversation data"
                )
                return

            # Anyone in AWAITING_AVAILABILITY => propose next slot
            awaiting = [ie for ie in conversation['interviewees']
                        if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value]
            for interviewee in awaiting:
                self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
                changed_something = True

            # Anyone in NO_SLOTS_AVAILABLE => check if new slots arrived that they haven't tried
            no_slots = [ie for ie in conversation['interviewees']
                        if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value]
            if not no_slots:
                continue
            available_slots = conversation.get('available_slots', [])
            reserved_slots = conversation.get('reserved_slots', [])
            updated_any = False

            for ie in no_slots:
                untried = self._get_untried_slots_for_interviewee(ie, available_slots, reserved_slots)
                if untried:
                    ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                    updated_any = True

            if updated_any:
                self.scheduler.mongodb_handler.update_conversation(
                    conversation['conversation_id'], {'interviewees': conversation['interviewees']}
                )
                changed_something = True

        # After we finish trying to fix states, check if there's anyone still unscheduled
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation after updating states",
                description="Cannot finalize process_remaining_interviewees due to missing conversation."
            )
            return

        unscheduled = [
            ie for ie in conversation['interviewees']
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]
        ]

        # If any are still pending confirmation, no need to prompt for more slots yet
        pending = [ie for ie in unscheduled if ie['state'] == ConversationState.CONFIRMATION_PENDING.value]
        if pending:
            logger.info("Some interviewees are in CONFIRMATION_PENDING; scheduling can continue in parallel.")
            return

        # If we still have unscheduled interviewees but no immediate next step, we may need more slots
        if unscheduled:
            requests_count = conversation.get('more_slots_requests', 0)
            # Compare to our new configurable MAX_SLOT_REQUESTS
            if requests_count >= MAX_SLOT_REQUESTS:
                logger.info("Reached maximum number of requests for more slots. Finalizing conversation.")
                # Create attention flag if you want to track that we gave up after repeated requests
                self._create_conversation_attention_flag(
                    conversation_id,
                    title="No More Slots Requested Over Limit",
                    description=(
                        f"Reached maximum number ({MAX_SLOT_REQUESTS}) of requests for more slots. "
                        "Finalizing conversation."
                    )
                )
                self.complete_conversation(conversation_id)
            else:
                self._request_more_slots(conversation_id, unscheduled, conversation)
        else:
            # Everyone is scheduled or canceled
            self.complete_conversation(conversation_id)

    def process_scheduling_for_interviewee(self, conversation_id: str, interviewee_number: str):
        """
        Attempts to propose the next untried slot to the interviewee. 
        If none are available, sets them to NO_SLOTS_AVAILABLE and checks next steps.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in process_scheduling_for_interviewee",
                description=f"Cannot schedule because conversation {conversation_id} missing."
            )
            return

        # If the conversation is completed, do not proceed.
        if conversation.get('status') == 'completed':
            logger.info(f"Skipping scheduling for interviewee {interviewee_number} in a completed conversation.")
            return

        interviewee = next((ie for ie in conversation['interviewees']
                            if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="Interviewee Not Found",
                description=f"Interviewee {interviewee_number} not found for scheduling in conversation {conversation_id}."
            )
            return

        # If this interviewee is currently waiting for them to confirm or deny a slot, skip
        if interviewee['state'] == ConversationState.CONFIRMATION_PENDING.value:
            return

        available_slots = conversation.get('available_slots', [])
        reserved_slots = conversation.get('reserved_slots', [])

        untried = self._get_untried_slots_for_interviewee(interviewee, available_slots, reserved_slots)
        if untried:
            next_slot = untried[0]
            interviewee['proposed_slot'] = next_slot
            interviewee['state'] = ConversationState.CONFIRMATION_PENDING.value
            interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [next_slot]
            reserved_slots.append(next_slot)

            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees'],
                'reserved_slots': reserved_slots
            })

            # Send a proposal message to the interviewee with local time
            timezone_str = interviewee.get('timezone', 'UTC')
            localized_start_time = datetime.fromisoformat(next_slot['start_time']).astimezone(
                pytz.timezone(timezone_str)
            ).strftime('%A, %B %d, %Y at %I:%M %p %Z')
            local_now = get_localized_current_time(timezone_str)

            system_message = (
                f"Instruct the AI assistant to propose to {interviewee['name']} the time slot "
                f"{localized_start_time} and ask if it works for them.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(
                interviewee,
                None,
                "",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)
        else:
            # No untried slots remain
            interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            logger.info(f"Interviewee {interviewee['name']} has no more untried slots; marking NO_SLOTS_AVAILABLE.")
            self.process_remaining_interviewees(conversation_id)

    def _get_untried_slots_for_interviewee(self, interviewee: dict, available_slots: list, reserved_slots: list) -> list:
        """
        Returns the subset of available_slots that have not been offered to 
        this interviewee and are not currently reserved by another interviewee.
        """
        offered_keys = {self._create_slot_key(slot) for slot in interviewee.get('offered_slots', [])}
        reserved_keys = {self._create_slot_key(slot) for slot in reserved_slots}
        return [
            slot for slot in available_slots
            if (self._create_slot_key(slot) not in offered_keys) and
               (self._create_slot_key(slot) not in reserved_keys)
        ]

    def _request_more_slots(self, conversation_id: str, unscheduled: list, conversation: dict):
        """
        Requests additional slots from the interviewer if not exceeding the maximum limit. 
        """
        interviewer = conversation.get('interviewer')
        if not interviewer:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Interviewer Found",
                description="Cannot request more slots because interviewer data is missing."
            )
            return

        # If everything is actually scheduled, do not request more slots
        if self.scheduler.is_conversation_complete(conversation):
            self.complete_conversation(conversation_id)
            return

        interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
        conversation['more_slots_requests'] = conversation.get('more_slots_requests', 0) + 1
        conversation['last_more_slots_request_time'] = datetime.now(pytz.UTC).isoformat()

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewer': interviewer,
            'more_slots_requests': conversation['more_slots_requests'],
            'last_more_slots_request_time': conversation['last_more_slots_request_time']
        })

        tz_str = interviewer.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)
        unscheduled_names = [ie['name'] for ie in unscheduled]

        system_message = (
            "Instruct the AI assistant to inform the interviewer that all current slots have been tried, "
            f"and the following interviewees remain unscheduled: {', '.join(unscheduled_names)}. "
            "Request the interviewer to provide more availability.\n\n"
            f"Current Local Time: {local_now}"
        )
        response = self.generate_response(
            interviewer,
            None,
            "",
            system_message,
            conversation_state=interviewer['state']
        )
        self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.send_message(interviewer['number'], response)

    def complete_conversation(self, conversation_id: str):
        """
        Marks conversation as completed & notifies the interviewer. 
        Then defers final closure tasks to the InterviewScheduler.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found.")
                self._create_conversation_attention_flag(
                    conversation_id,
                    title="No Conversation in complete_conversation",
                    description=f"Cannot complete conversation {conversation_id} as not found."
                )
                return

            unscheduled = [
                ie['name'] for ie in conversation['interviewees']
                if ie['state'] in [ConversationState.NO_SLOTS_AVAILABLE.value,
                                   ConversationState.AWAITING_AVAILABILITY.value,
                                   ConversationState.CONFIRMATION_PENDING.value]
            ]

            conversation['status'] = 'completed'
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'status': 'completed'
            })

            interviewer = conversation['interviewer']
            tz_str = interviewer.get('timezone', 'UTC')
            local_now = get_localized_current_time(tz_str)

            if unscheduled:
                note = f"Some interviewees could not be scheduled: {', '.join(unscheduled)}."
            else:
                note = "All interviews have been successfully scheduled."

            system_message = (
                "Instruct the AI assistant to inform the interviewer that all scheduling steps have been completed. "
                f"{note} Thank them for their cooperation.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(
                interviewer,
                None,
                "",
                system_message,
                conversation_state=ConversationState.COMPLETED.value
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

            # Let the InterviewScheduler handle final summary emails/notifications
            self.scheduler.complete_conversation(conversation_id)

        except Exception as e:
            logger.error(f"Error completing conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())
            self._create_conversation_attention_flag(
                conversation_id,
                title="Error Completing Conversation",
                description=f"Exception: {str(e)}"
            )

    def send_reminder(self, conversation_id: str, participant_id: str):
        """
        Sends a reminder to the given participant if the conversation is still active.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for sending reminder.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="Reminder Error",
                description=f"No conversation found for {conversation_id} when trying to send reminder."
            )
            return

        if participant_id == 'interviewer':
            participant = conversation['interviewer']
        else:
            participant = next((ie for ie in conversation['interviewees']
                                if ie['number'] == participant_id), None)

        if not participant:
            logger.error(f"Participant {participant_id} not found in conversation {conversation_id}.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="Reminder Error",
                description=f"Participant {participant_id} not found in conversation {conversation_id}."
            )
            return

        if conversation.get('status') == 'completed':
            logger.info(f"No reminder sent; conversation {conversation_id} is completed.")
            return

        tz_str = participant.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        system_message = (
            f"Instruct the AI assistant to send a reminder to {participant['name']} that no response has been received, "
            f"and request an update regarding scheduling.\n\n"
            f"Current Local Time: {local_now}"
        )
        response = self.generate_response(
            participant,
            None,
            "",
            system_message,
            conversation_state=participant.get('state')
        )
        self.scheduler.log_conversation(conversation_id, participant_id, "system", response, "AI")
        self.send_message(participant['number'], response)

    def update_participant_timezone(self, conversation_id: str, participant: dict, timezone: str) -> None:
        """
        Updates a participant's timezone, then asks them to provide availability if needed.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                self._create_conversation_attention_flag(
                    conversation_id,
                    title="Timezone Update Error",
                    description=f"Conversation {conversation_id} not found."
                )
                return

            if participant.get('role') == 'interviewer':
                conversation['interviewer']['timezone'] = timezone
                conversation['interviewer']['state'] = ConversationState.AWAITING_AVAILABILITY.value
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': conversation['interviewer']
                })
            else:
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == participant['number']:
                        ie['timezone'] = timezone
                        ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                        conversation['interviewees'][i] = ie
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

            local_now = get_localized_current_time(timezone)
            system_message = (
                f"Instruct the AI assistant to acknowledge that {participant['name']}'s timezone has been set to {timezone} "
                f"and request them to provide availability for scheduling.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(
                participant,
                None,
                "",
                system_message,
                conversation_state=ConversationState.AWAITING_AVAILABILITY.value
            )
            self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
            self.send_message(participant['number'], response)

        except Exception as e:
            logger.error(f"Error updating timezone for participant {participant['number']}: {str(e)}")
            logger.error(traceback.format_exc())
            self._create_conversation_attention_flag(
                conversation_id,
                title="Error Updating Timezone",
                description=f"Exception: {str(e)}"
            )

    def initiate_conversation_with_interviewee(self, conversation_id: str, interviewee_number: str):
        """
        Starts the conversation flow with a newly added interviewee. 
        Tries to detect timezone automatically; if none is found, asks them for it.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="Missing Conversation in initiate_conversation_with_interviewee",
                description=f"Cannot initiate conversation for {interviewee_number}."
            )
            return

        if conversation.get('status') == 'completed':
            logger.info(f"Skipping conversation initiation for {interviewee_number} in completed conversation.")
            return

        interviewee = next((ie for ie in conversation['interviewees']
                            if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="Interviewee Not Found",
                description=f"Cannot initiate for interviewee {interviewee_number} in conversation {conversation_id}."
            )
            return

        # Attempt to auto-detect the interviewee's timezone from phone number
        interviewee_timezone = extract_timezone_from_number(interviewee['number'])
        if interviewee_timezone and interviewee_timezone.lower() != 'unspecified':
            interviewee['timezone'] = interviewee_timezone
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })
            # Proceed with scheduling if we already have the timezone
            self.process_scheduling_for_interviewee(conversation_id, interviewee_number)
        else:
            # If we do not know their timezone, ask for it
            interviewee['state'] = ConversationState.TIMEZONE_CLARIFICATION.value
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            local_now = get_localized_current_time('UTC')
            system_message = (
                f"Instruct the AI assistant to ask {interviewee['name']} for their timezone to proceed with scheduling.\n\n"
                f"Current Local Time (fallback UTC): {local_now}"
            )
            response = self.generate_response(
                interviewee,
                None,
                "Null",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee_number, "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def initiate_scheduling_for_no_slots_available(self, conversation_id: str):
        """
        Called when the interviewer has just provided new slots, to move 
        interviewees in NO_SLOTS_AVAILABLE to see if we can now schedule them.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in initiate_scheduling_for_no_slots_available",
                description="Cannot initiate scheduling for no slots."
            )
            return

        if conversation.get('status') == 'completed':
            logger.info(f"Skipping scheduling for NO_SLOTS_AVAILABLE in conversation {conversation_id} (completed).")
            return

        no_slots_interviewees = [
            ie for ie in conversation['interviewees']
            if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value
        ]
        if not no_slots_interviewees:
            logger.info(f"No interviewees with NO_SLOTS_AVAILABLE in conversation {conversation_id}.")
            return

        for ie in no_slots_interviewees:
            self.process_scheduling_for_interviewee(conversation_id, ie['number'])

    def initiate_scheduling_for_awaiting_availability(self, conversation_id: str):
        """
        Called when the interviewer has just provided new slots, to re-trigger scheduling 
        for interviewees who are awaiting availability.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in initiate_scheduling_for_awaiting_availability",
                description="Cannot re-trigger scheduling for interviewees awaiting availability."
            )
            return

        if conversation.get('status') == 'completed':
            logger.info(f"Skipping scheduling for AWAITING_AVAILABILITY in conversation {conversation_id} (completed).")
            return

        awaiting = [
            ie for ie in conversation['interviewees']
            if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value
        ]
        if not awaiting:
            logger.info(f"No interviewees with AWAITING_AVAILABILITY in conversation {conversation_id}.")
            return

        for interviewee in awaiting:
            self.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])

    def handle_query(self, conversation_id: str, participant: dict, message: str):
        """
        Handles generic queries from participants, responding via LLM's 'answer_query' method.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_query",
                description=f"No conversation found for {conversation_id} while handling query."
            )
            return

        other_participant = None
        if participant.get('role') != 'interviewer':
            other_participant = conversation.get('interviewer')

        tz_str = participant.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        system_message = (
            "Instruct the AI assistant to address the participant's query and provide a helpful response.\n\n"
            f"Current Local Time: {local_now}"
        )

        response = self.generate_response(
            participant,
            other_participant,
            message,
            system_message=system_message,
            conversation_state=participant.get('state'),
            message_type='answer_query'
        )
        self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_cancellation_request_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        """
        Handles cancellation requests from the interviewer, either for a specific interviewee 
        or overall. Prompts for the interviewee name if not provided.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_cancellation_request_interviewer",
                description=f"Cannot handle cancellation because conversation {conversation_id} is missing."
            )
            return

        state = interviewer.get('state')
        tz_str = interviewer.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        if state == ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value:
            # The interviewer is supposed to name the interviewee whose meeting they want to cancel
            interviewee_name = message.strip().lower()
            interviewee = next((ie for ie in conversation.get('interviewees', [])
                                if ie['name'].lower() == interviewee_name), None)

            if not interviewee:
                system_message = (
                    f"Instruct the AI assistant to inform the interviewer that no interviewee named '{interviewee_name}' was found.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)
                if delete_success:
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            ie['event_id'] = None
                            ie['state'] = ConversationState.CANCELLED.value
                            conversation['interviewees'][i] = ie
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    cancel_message = (
                        f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled."
                    )
                    self.send_message(interviewer['number'], cancel_message)
                    self.send_message(interviewee['number'], cancel_message)

                    system_message = (
                        f"Instruct the AI assistant to confirm for the interviewer that the meeting with "
                        f"{interviewee['name']} has been cancelled.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    system_message = (
                        "Instruct the AI assistant to inform the interviewer that the cancellation failed due to an internal error.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    self._create_conversation_attention_flag(
                        conversation_id,
                        title="Cancellation Failed",
                        description=f"Failed to delete calendar event {event_id} for {interviewee_name}."
                    )
            else:
                system_message = (
                    f"Instruct the AI assistant to inform the interviewer that no scheduled meeting was found for {interviewee['name']}.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

            interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            # Check if the conversation can be completed after this cancellation
            if self.scheduler.is_conversation_complete(conversation):
                self.complete_conversation(conversation_id)

        else:
            # We haven't asked them to specify which interviewee yet
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = (
                "Instruct the AI assistant to ask the interviewer for the name of the interviewee whose meeting "
                "they wish to cancel.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_cancellation_request_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        """
        Handles cancellation requests from an interviewee. Tries to extract the interviewee name 
        from the message, then cancels if there's a matching scheduled event.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_cancellation_request_interviewee",
                description=f"Cannot handle interviewee cancellation for {conversation_id}."
            )
            return

        interviewer = conversation.get('interviewer')
        tz_str = interviewee.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        extracted_name = self.llm_model.extract_interviewee_name(message)
        if extracted_name:
            interviewee_obj = next(
                (ie for ie in conversation['interviewees']
                 if ie['name'].lower() == extracted_name.lower()),
                None
            )
            if interviewee_obj:
                event_id = interviewee_obj.get('event_id')
                if event_id:
                    delete_success = self.scheduler.calendar_service.delete_event(event_id)
                    if delete_success:
                        for i, ie in enumerate(conversation['interviewees']):
                            if ie['number'] == interviewee_obj['number']:
                                ie['event_id'] = None
                                ie['state'] = ConversationState.CANCELLED.value
                                conversation['interviewees'][i] = ie
                        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                            'interviewees': conversation['interviewees']
                        })

                        cancel_message = (
                            f"The meeting between {interviewer['name']} and {interviewee_obj['name']} has been cancelled."
                        )
                        self.send_message(interviewer['number'], cancel_message)
                        self.send_message(interviewee_obj['number'], cancel_message)

                        system_message = (
                            f"Instruct the AI assistant to confirm that the meeting with {interviewee_obj['name']} was cancelled.\n\n"
                            f"Current Local Time: {local_now}"
                        )
                        response = self.generate_response(interviewee_obj, None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)
                    else:
                        system_message = (
                            "Instruct the AI assistant to inform the participant that the cancellation failed due to an internal error.\n\n"
                            f"Current Local Time: {local_now}"
                        )
                        response = self.generate_response(interviewee_obj, None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)

                        self._create_conversation_attention_flag(
                            conversation_id,
                            title="Cancellation Failed",
                            description=f"Failed to delete event {event_id} for {interviewee_obj['name']}."
                        )
                else:
                    system_message = (
                        f"Instruct the AI assistant to inform the participant that no scheduled meeting was found for {interviewee_obj['name']}.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewee_obj, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                    self.send_message(interviewee_obj['number'], response)
            else:
                system_message = (
                    f"Instruct the AI assistant to inform the interviewee that no interviewee named '{extracted_name}' was found.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            # We couldn't parse the name, ask them for it
            interviewee['state'] = ConversationState.AWAITING_INTERVIEWEE_NAME.value
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee['number']:
                    conversation['interviewees'][i] = interviewee
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            system_message = (
                "Instruct the AI assistant to ask the interviewee for the name of the interviewee whose interview "
                "they wish to cancel.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_reschedule_request_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        """
        Handles a rescheduling request from an interviewer. If there's exactly one scheduled 
        interviewee, tries to reschedule that automatically; otherwise asks which interviewee.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_reschedule_request_interviewer",
                description=f"Cannot handle reschedule for conversation {conversation_id}."
            )
            return

        scheduled = [ie for ie in conversation['interviewees'] if ie.get('event_id')]
        tz_str = interviewer.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        if not scheduled:
            system_message = (
                "Instruct the AI assistant to inform the interviewer that no scheduled meeting was found to reschedule.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        if len(scheduled) == 1:
            # If exactly one interviewee is scheduled, attempt immediate rescheduling
            target_ie = scheduled[0]
            event_id = target_ie.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)
                if delete_success:
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == target_ie['number']:
                            ie['event_id'] = None
                            ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                            ie['reschedule_count'] = ie.get('reschedule_count', 0) + 1
                            conversation['interviewees'][i] = ie
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    system_message = (
                        f"Instruct the AI assistant to inform the interviewer that the meeting with {target_ie['name']} "
                        f"is being rescheduled and to proceed with collecting new availability.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    # Immediately move on to re-propose slots for that interviewee
                    self.process_scheduling_for_interviewee(conversation_id, target_ie['number'])
                else:
                    system_message = (
                        "Instruct the AI assistant to inform the interviewer that the rescheduling failed due to an internal error.\n\n"
                        f"Current Local Time: {local_now}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    self._create_conversation_attention_flag(
                        conversation_id,
                        title="Reschedule Failed",
                        description=f"Failed to delete event {event_id} for {target_ie['name']} in interviewer reschedule."
                    )
            else:
                system_message = (
                    f"Instruct the AI assistant to inform the interviewer that no scheduled meeting was found for {target_ie['name']}.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
        else:
            # Multiple interviewees are scheduled, so we need to ask which one
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = (
                "Instruct the AI assistant to ask the interviewer which interviewee's meeting they wish to reschedule, "
                "since multiple interviews are scheduled.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_reschedule_request_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        """
        Handles a rescheduling request from an interviewee. If there's an event_id, 
        we delete and set them back to AWAITING_AVAILABILITY.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            self._create_conversation_attention_flag(
                conversation_id,
                title="No Conversation in handle_reschedule_request_interviewee",
                description=f"Cannot handle reschedule for conversation {conversation_id}."
            )
            return

        event_id = interviewee.get('event_id')
        tz_str = interviewee.get('timezone', 'UTC')
        local_now = get_localized_current_time(tz_str)

        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)
            if delete_success:
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee['number']:
                        ie['event_id'] = None
                        ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                        ie['reschedule_count'] = ie.get('reschedule_count', 0) + 1
                        conversation['interviewees'][i] = ie
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                system_message = (
                    "Instruct the AI assistant to inform the interviewee that the rescheduling failed due to an internal error.\n\n"
                    f"Current Local Time: {local_now}"
                )
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)

                self._create_conversation_attention_flag(
                    conversation_id,
                    title="Reschedule Failed",
                    description=f"Failed to delete event {event_id} for interviewee {interviewee['name']}."
                )
        else:
            system_message = (
                "Instruct the AI assistant to inform the interviewee that no scheduled meeting was found to reschedule.\n\n"
                f"Current Local Time: {local_now}"
            )
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

        if self.scheduler.is_conversation_complete(conversation):
            self.complete_conversation(conversation_id)

    def _create_slot_key(self, slot):
        """
        Helper method to produce a unique key for a given slot, 
        typically its start_time. Useful to avoid duplicates.
        """
        if not slot:
            logger.error("Invalid slot: slot is None or empty")
            return None
        if 'start_time' not in slot:
            logger.error(f"Invalid slot format: missing start_time in slot {slot}")
            return None

        key = f"{slot['start_time']}"
        logger.debug(f"Created slot key: {key} for slot: {slot}")
        return key

    # -------------------------------------------------------------------------
    # NEW: Below are helper methods for creating attention flags in various scenarios
    # -------------------------------------------------------------------------

    def _create_general_attention_flag(self, title: str, description: str):
        """
        Creates a generic (not conversation-specific) attention flag, if the method exists.
        If your mongodb_handler requires certain fields, adapt accordingly.
        """
        try:
            # Many implementations use something like:
            # self.scheduler.mongodb_handler.create_attention_flag(...)
            # We'll assume it accepts a dictionary or named params:
            self.scheduler.mongodb_handler.create_attention_flag(
                flag_data={
                    "title": title,
                    "description": description,
                    "resolved": False,
                    "created_at": datetime.utcnow().isoformat()
                }
            )
        except Exception as e:
            logger.error(f"Failed to create general attention flag: {str(e)}")

    def _create_conversation_attention_flag(self, conversation_id: str, title: str, description: str):
        """
        Creates a conversation-specific attention flag in the database.
        """
        try:
            self.scheduler.mongodb_handler.create_attention_flag(
                conversation_id=conversation_id,
                flag_data={
                    "title": title,
                    "description": description,
                    "resolved": False,
                    "created_at": datetime.utcnow().isoformat()
                }
            )
        except Exception as e:
            logger.error(f"Failed to create attention flag for conversation {conversation_id}: {str(e)}")
