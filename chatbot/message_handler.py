# chatbot/message_handler.py
import logging
import random
import time
import os
from datetime import datetime, timedelta
import pytz
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from chatbot.constants import ConversationState, AttentionFlag
from chatbot.utils import extract_timezone_from_city, extract_city_from_message, extract_slots_and_timezone, normalize_number, extract_timezone_from_number, get_localized_current_time
from dotenv import load_dotenv
from .llm.llmmodel import LLMModel
import traceback
from typing import Dict, Any

load_dotenv()

logger = logging.getLogger(__name__)

class MessageHandler:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.llm_model = LLMModel()

    def send_message(self, to_number: str, message: str, max_retries: int = 3, initial_retry_delay: float = 1.0) -> bool:
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'

        twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        twilio_whatsapp_number = os.getenv('TWILIO_WHATSAPP_NUMBER')

        if not all([twilio_account_sid, twilio_auth_token, twilio_whatsapp_number]):
            logger.error("Missing Twilio credentials. Please check environment variables.")
            return False

        client = Client(twilio_account_sid, twilio_auth_token)
        retry_count = 0
        current_delay = initial_retry_delay
        last_exception = None

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

                if e.code in [20003, 20426]:
                    logger.error("Authentication failed. Please check Twilio credentials.")
                    return False
                elif e.code in [21211, 21614]:
                    logger.error(f"Invalid phone number: {to_number}")
                    return False
                elif e.code == 21617:
                    logger.error("Message exceeds maximum length")
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
                    f"Failed to send message to {to_number} after {max_retries} attempts. Last error: {str(last_exception)}"
                )
                return False

            jitter = random.uniform(0, 0.1) * current_delay
            sleep_time = current_delay + jitter
            logger.debug(f"Retrying in {sleep_time:.2f} seconds...")
            time.sleep(sleep_time)
            current_delay = min(current_delay * 2, 30)

        return False

    def generate_response(
        self,
        participant: dict,
        other_participant: dict,
        user_message: str,
        system_message: str,
        conversation_state: str = None,
        message_type: str = 'generate_message'
    ) -> str:
        conversation_state = conversation_state or participant.get('state')
        conversation_history = " ".join(participant.get('conversation_history', []))
        other_conversation_history = ""
        if other_participant:
            other_conversation_history = " ".join(other_participant.get('conversation_history', []))

        params = {
            'participant_name': participant['name'],
            'participant_number': participant['number'],
            'participant_email': participant['email'],
            'participant_role': participant['role'],
            'superior_flag': participant.get('superior_flag', False),
            'meeting_duration': participant.get('meeting_duration'),
            'role_to_contact_name': participant.get('role_to_contact_name'),
            'role_to_contact_number': participant.get('role_to_contact_number'),
            'role_to_contact_email': participant.get('role_to_contact_email'),
            'company_details': participant.get('company_details'),
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message,
            'system_message': system_message
        }

        if message_type == 'generate_message':
            response = self.llm_model.generate_message(**params)
        # elif message_type == 'generate_conversational_message':
        #     response = self.llm_model.generate_conversational_message(**params)
        elif message_type == 'answer_query':
            response = self.llm_model.answer_query(**params)
        else:
            raise ValueError(f"Unknown message_type: {message_type}")

        return response

    def receive_message(self, from_number, message):
        conversation_id, participant, interviewer_number = self.find_conversation_and_participant(from_number, message)
        if not conversation_id or not participant:
            logger.warning(f"No active conversation found for number: {from_number}")
            return

        # Update last response time
        now_utc = datetime.now(pytz.UTC)
        self.scheduler.mongodb_handler.update_conversation(
            conversation_id, 
            {f'last_response_times.{participant["number"]}': now_utc.isoformat()}
        )

        self.scheduler.log_conversation_history(conversation_id)
        self.scheduler.log_conversation(conversation_id, participant['number'], "user", message, "Participant")

        # Reload conversation from DB
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)

        intent = self.llm_model.detect_intent(
            participant_name=participant['name'],
            participant_role=participant['role'],
            meeting_duration=participant['meeting_duration'],
            role_to_contact=participant.get('role_to_contact_name'),
            conversation_history=" ".join(participant['conversation_history']),
            conversation_state=participant.get('state'),
            user_message=message
        )

        logger.info(f"Detected intent: {intent}")

        if "CANCELLATION_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_cancellation_request_interviewer(conversation_id, participant, message)
            else:
                self.handle_cancellation_request_interviewee(conversation_id, participant, message)

        elif "QUERY" in intent:
            self.handle_query(conversation_id, participant, message)

        elif "RESCHEDULE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_reschedule_request_interviewer(conversation_id, participant, message)
            else:
                self.handle_reschedule_request_interviewee(conversation_id, participant, message)
        else:
            # Regular flow
            if participant['role'] == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def find_conversation_and_participant(self, from_number: str, message: str):
        """
        Locate the conversation and participant by a given phone number.
        Since participants can be in multiple conversations, especially interviewers,
        we need to determine which conversation to associate with the incoming message.

        Strategy:
        - If the message is related to an action (e.g., cancel, reschedule), use the interviewer's number mentioned in the message.
        - Otherwise, prioritize active conversations.
        - If multiple active conversations exist, prompt the user to specify which one they're referring to.
        """

        from_number_norm = normalize_number(from_number)
        # Query DB for conversations including this number
        conversations = self.scheduler.mongodb_handler.find_conversations_by_number(from_number_norm)

        if not conversations:
            return None, None, None

        if len(conversations) == 1:
            conversation = conversations[0]
            participant = conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm else next(
                (ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        # Multiple conversations found
        # Attempt to identify based on the context or message content
        # For simplicity, we'll assume the latest active conversation is the target
        active_conversations = [c for c in conversations if c['status'] == 'active']
        if active_conversations:
            # Select the latest active conversation
            conversation = sorted(active_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm else next(
                (ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        # If no active conversations, select the latest queued conversation
        queued_conversations = [c for c in conversations if c['status'] == 'queued']
        if queued_conversations:
            conversation = sorted(queued_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm else next(
                (ie for ie in conversation['interviewees'] if ie['number'] == from_number_norm), None)
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        # If no conversations match, return None
        return None, None, None

    def handle_message_from_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if interviewer['state'] == ConversationState.AWAITING_SLOT_CONFIRMATION.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewer['name'],
                participant_role=interviewer['role'],
                meeting_duration=interviewer['meeting_duration'],
                conversation_history=" ".join(interviewer['conversation_history']),
                conversation_state=interviewer['state'],
                user_message=message
            )

            if confirmation_response.get('confirmed'):
                temp_slots = interviewer.get('temp_slots')
                if not temp_slots:
                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    system_message = f"Error with the time slots. Please provide availability again.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                    return

                interviewer['slots'] = temp_slots
                interviewer['temp_slots'] = None
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"Thanks for confirming. We'll proceed with scheduling.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                # Initiate with the first awaiting interviewee
                conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
                for ie in conversation['interviewees']:
                    if (ie['state'] == ConversationState.AWAITING_AVAILABILITY.value):
                        self.initiate_conversation_with_interviewee(conversation_id, ie['number'])
                        break
            else:
                extracted_data = extract_slots_and_timezone(
                    message,
                    interviewer['number'],
                    interviewer['conversation_history'],
                    interviewer['meeting_duration']
                )

                if extracted_data and 'time_slots' in extracted_data:
                    interviewer['temp_slots'] = extracted_data
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    formatted_slots = []
                    for slot in extracted_data.get("time_slots", []):
                        start_time = datetime.fromisoformat(slot['start_time'])
                        timezone = extracted_data.get('timezone', 'UTC')
                        formatted_slots.append(
                            f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                        )

                    slots_text = "\n".join(formatted_slots)

                    # Get localized current time
                    current_time = get_localized_current_time(timezone)

                    system_message = f"New time slots identified:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    interviewer['temp_slots'] = None
                    interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    system_message = f"Please provide availability in a clear format.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

        elif interviewer['state'] == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and 'time_slots' in extracted_data:
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)

                # Get localized current time
                current_time = get_localized_current_time(timezone)

                system_message = f"Identified new time slots:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return
            else:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"Could not understand your availability. Please provide it in a clear format.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

        else:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and 'time_slots' in extracted_data:
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)

                # Get localized current time
                current_time = get_localized_current_time(timezone)

                system_message = f"Identified these time slots:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
            else:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"Could not understand your availability. Please provide it in a clear format.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

    def initiate_conversation_with_interviewee(self, conversation_id, interviewee_number):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            return

        interviewer = conversation['interviewer']
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)

        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        interviewee_timezone = extract_timezone_from_number(interviewee['number'])
        if interviewee_timezone and interviewee_timezone.lower() != 'unspecified':
            interviewee['timezone'] = interviewee_timezone
            # Update interviewee in DB
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })
            self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee_number)
        else:
            interviewee['state'] = ConversationState.TIMEZONE_CLARIFICATION.value
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            # Get localized current time
            timezone_str = 'UTC'
            current_time = get_localized_current_time(timezone_str)

            system_message = f"Hello {interviewee['name']}, please let me know your timezone to proceed.\n\nCurrent Time: {current_time}"
            response = self.generate_response(
                interviewee,
                None,
                "Null",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee_number, "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_message_from_interviewee(self, conversation_id, interviewee, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        interviewer = conversation.get('interviewer')

        if not interviewee.get('timezone'):
            extracted_data = extract_slots_and_timezone(
                message,
                interviewee['number'],
                interviewee['conversation_history'],
                interviewee['meeting_duration']
            )
            timezone = extracted_data.get('timezone')
            if timezone and timezone.lower() != 'unspecified':
                interviewee['timezone'] = timezone
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee['number']:
                        conversation['interviewees'][i] = interviewee

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                # Get localized current time
                timezone_str = 'UTC'
                current_time = get_localized_current_time(timezone_str)

                system_message = f"Please specify your timezone.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewee,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        elif interviewee['state'] == ConversationState.CONFIRMATION_PENDING.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewee['name'],
                participant_role=interviewee['role'],
                meeting_duration=interviewee['meeting_duration'],
                conversation_history=" ".join(interviewee['conversation_history']),
                conversation_state=interviewee['state'],
                user_message=message
            )
            if confirmation_response.get('confirmed'):
                interviewee['confirmed'] = True
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee['number']:
                        conversation['interviewees'][i] = interviewee

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                # Archive the denied slot
                proposed_slot = interviewee.get('proposed_slot')
                if proposed_slot:
                    archived_slots = conversation.get('archived_slots', [])
                    archived_slots.append(proposed_slot)
                    interviewee['proposed_slot'] = None
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            conversation['interviewees'][i] = ie

                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'archived_slots': archived_slots,
                        'interviewees': conversation['interviewees']
                    })

                # Find next unscheduled interviewee who hasn't been offered all slots
                next_interviewee = None
                available_slots = conversation.get('available_slots', [])
                for ie in conversation['interviewees']:
                    if (ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value, 
                                        ConversationState.NO_SLOTS_AVAILABLE.value] and
                        ie['number'] != interviewee['number']):
                        offered_slots = ie.get('offered_slots', [])
                        if any(slot not in offered_slots for slot in available_slots):
                            next_interviewee = ie
                            break

                if next_interviewee:
                    # Process scheduling for next interviewee
                    self.scheduler.process_scheduling_for_interviewee(conversation_id, next_interviewee['number'])
                else:
                    # All slots have been offered to all interviewees, now ask interviewer for more slots
                    # Mark current interviewee as NO_SLOTS_AVAILABLE
                    interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            conversation['interviewees'][i] = interviewee

                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    # Ask interviewer for more slots
                    interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer,
                        'asked_for_more_slots': True
                    })

                    # Notify interviewer
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    
                    # Get list of unscheduled interviewees
                    unscheduled = [ie['name'] for ie in conversation['interviewees'] 
                                if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value]
                    
                    system_message = (
                        f"All available slots have been offered to interviewees. The following interviewees "
                        f"could not be scheduled: {', '.join(unscheduled)}. Could you please provide more "
                        f"availability?\n\nCurrent Time: {current_time}"
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
        else:
            # Handle other states or regular messages
            pass

    def handle_query(self, conversation_id, participant, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if participant['role'] == 'interviewer':
            other_participant = None
        else:
            other_participant = conversation.get('interviewer')

        # Get localized current time
        timezone_str = participant.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        response = self.generate_response(
            participant,
            None,
            message,
            system_message=f"Your query has been received.\n\nCurrent Time: {current_time}",
            conversation_state=participant.get('state'),
            message_type='answer_query'
        )

        self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_cancellation_request_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        state = interviewer.get('state')

        if state == ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value:
            interviewee_name = message.strip().lower()
            interviewee = next((ie for ie in conversation.get('interviewees', []) 
                                if ie['name'].lower() == interviewee_name), None)

            if not interviewee:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"No interviewee named '{interviewee_name}' found.\n\nCurrent Time: {current_time}"
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)
                if delete_success:
                    # Clear event_id and update interviewee state
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            ie['event_id'] = None
                            ie['state'] = ConversationState.CANCELLED.value
                            conversation['interviewees'][i] = ie
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    cancel_message = f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled.\n\nCurrent Time: {current_time}"
                    self.send_message(interviewer['number'], cancel_message)
                    self.send_message(interviewee['number'], cancel_message)

                    system_message = f"Cancelled the meeting with {interviewee['name']}.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    system_message = f"Failed to cancel due to an internal error.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"No scheduled meeting found with that interviewee.\n\nCurrent Time: {current_time}"
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

            interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            # Check if conversation is complete
            if self.is_conversation_complete(conversation):
                self.complete_conversation(conversation_id)

        else:
            # Get localized current time
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = f"Please provide the name of the interviewee to cancel.\n\nCurrent Time: {current_time}"
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_cancellation_request_interviewee(self, conversation_id, interviewee, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        interviewer = conversation.get('interviewer')

        # Step 1: Extract interviewee's name from the message
        extracted_name = self.llm_model.extract_interviewee_name(message)

        if extracted_name:
            # Step 2: Validate the extracted name
            interviewee_obj = next(
                (ie for ie in conversation['interviewees'] if ie['name'].lower() == extracted_name.lower()),
                None
            )

            if interviewee_obj:
                event_id = interviewee_obj.get('event_id')
                if event_id:
                    delete_success = self.scheduler.calendar_service.delete_event(event_id)
                    if delete_success:
                        # Clear event_id and update interviewee state
                        for i, ie in enumerate(conversation['interviewees']):
                            if ie['number'] == interviewee_obj['number']:
                                ie['event_id'] = None
                                ie['state'] = ConversationState.CANCELLED.value
                                conversation['interviewees'][i] = ie
                        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                            'interviewees': conversation['interviewees']
                        })

                        # Get localized current time
                        timezone_str = interviewer.get('timezone', 'UTC')
                        current_time = get_localized_current_time(timezone_str)

                        cancel_message = f"The meeting between {interviewer['name']} and {interviewee_obj['name']} has been cancelled.\n\nCurrent Time: {current_time}"
                        self.send_message(interviewer['number'], cancel_message)
                        self.send_message(interviewee_obj['number'], cancel_message)

                        system_message = f"Cancelled the meeting with {interviewee_obj['name']}.\n\nCurrent Time: {current_time}"
                        response = self.generate_response(interviewee_obj, None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)
                    else:
                        # Get localized current time
                        timezone_str = interviewer.get('timezone', 'UTC')
                        current_time = get_localized_current_time(timezone_str)

                        system_message = f"Failed to cancel due to an internal error.\n\nCurrent Time: {current_time}"
                        response = self.generate_response(interviewee_obj,None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)
                else:
                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    system_message = f"No scheduled meeting found with that interviewee.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(interviewee_obj, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                    self.send_message(interviewee_obj['number'], response)
            else:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                # Extracted name does not match any interviewee
                system_message = f"No interviewee found with that name. Please provide a valid interviewee's name to cancel.\n\nCurrent Time: {current_time}"
                response = self.generate_response(interviewee_obj, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                self.send_message(interviewee_obj['number'], response)
        else:
            # Step 3: Name not found in the message, prompt for it

            # Get localized current time
            timezone_str = interviewee.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            interviewee['state'] = ConversationState.AWAITING_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            system_message = f"Please provide the name of the interviewee whose interview you wish to cancel.\n\nCurrent Time: {current_time}"
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_reschedule_request_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        scheduled_interviewees = [ie for ie in conversation['interviewees'] if ie.get('event_id')]
        if not scheduled_interviewees:
            # Get localized current time
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            system_message = f"No scheduled meeting found to reschedule.\n\nCurrent Time: {current_time}"
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        if len(scheduled_interviewees) == 1:
            interviewee = scheduled_interviewees[0]
            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)
                if delete_success:
                    # Remove event_id and reset state to AWAITING_AVAILABILITY
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            ie['event_id'] = None
                            ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                            ie['reschedule_count'] = ie.get('reschedule_count', 0) + 1
                            conversation['interviewees'][i] = ie
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    # Attempt scheduling again
                    system_message = f"Rescheduling the meeting with {interviewee['name']}.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
                else:
                    # Get localized current time
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)

                    system_message = f"Failed to reschedule due to an internal error. Please try again later.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                # Get localized current time
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"No scheduled meeting found for that participant.\n\nCurrent Time: {current_time}"
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
        else:
            # Get localized current time
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = f"Multiple scheduled interviews found. Please provide the interviewee's name to reschedule.\n\nCurrent Time: {current_time}"
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_reschedule_request_interviewee(self, conversation_id, interviewee, message):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)

        event_id = interviewee.get('event_id')
        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)
            if delete_success:
                # Clear event_id and update interviewee state
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee['number']:
                        ie['event_id'] = None
                        ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                        ie['reschedule_count'] = ie.get('reschedule_count', 0) + 1
                        conversation['interviewees'][i] = ie
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                # Get localized current time
                timezone_str = interviewee.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)

                system_message = f"Failed to reschedule due to an internal error. Please try again later.\n\nCurrent Time: {current_time}"
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            # Get localized current time
            timezone_str = interviewee.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            system_message = f"No scheduled meeting found to reschedule.\n\nCurrent Time: {current_time}"
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

        # Check if conversation is complete
        if self.is_conversation_complete(conversation):
            self.complete_conversation(conversation_id)

    def is_conversation_complete(self, conversation: Dict[str, Any]) -> bool:
        """
        Determine if the conversation is complete.
        A conversation is complete if all interviewees are either scheduled or cancelled.
        """
        for ie in conversation['interviewees']:
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]:
                return False
        return True

    def send_reminder(self, conversation_id, participant_id):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for sending reminder.")
            return

        if participant_id == 'interviewer':
            participant = conversation['interviewer']
        else:
            participant = next((ie for ie in conversation['interviewees'] if ie['number'] == participant_id), None)

        if not participant:
            logger.error(f"Participant {participant_id} not found in conversation {conversation_id} for reminder.")
            return

        # Get localized current time
        timezone_str = participant.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        system_message = f"Hello, we haven't heard from you. Could you please provide an update?\n\nCurrent Time: {current_time}"
        response = self.generate_response(
            participant,
            None,
            "",
            system_message,
            conversation_state=participant['state']
        )
        self.scheduler.log_conversation(conversation_id, participant_id, "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_timezone_clarification_response(self, conversation_id: str, participant: dict, message: str) -> None:
        """
        Handle timezone clarification responses from participants.
        """
        try:
            # First try to extract city
            city = extract_city_from_message(message)
            if city and city.lower() != 'unspecified':
                timezone = extract_timezone_from_city(city)
                if timezone and timezone.lower() != 'unspecified':
                    self.update_participant_timezone(conversation_id, participant, timezone)
                    return

            # If city extraction fails, try to extract timezone from availability message
            extracted_data = extract_slots_and_timezone(
                message,
                participant['number'],
                participant['conversation_history'],
                participant['meeting_duration']
            )
            
            timezone = extracted_data.get('timezone')
            if timezone and timezone.lower() != 'unspecified':
                self.update_participant_timezone(conversation_id, participant, timezone)
                return

            # If all methods fail, ask for explicit timezone
            current_time = get_localized_current_time('UTC')
            system_message = (
                "I couldn't determine your timezone. Please provide your timezone explicitly "
                "(e.g., 'America/New_York', 'Asia/Kolkata', 'Europe/London').\n\n"
                f"Current Time: {current_time}"
            )
            response = self.generate_response(
                participant,
                None,
                "",
                system_message,
                conversation_state=ConversationState.TIMEZONE_CLARIFICATION.value
            )
            self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
            self.send_message(participant['number'], response)

        except Exception as e:
            logger.error(f"Error handling timezone clarification for participant {participant['number']}: {str(e)}")
            logger.error(traceback.format_exc())
            self.update_participant_timezone(conversation_id, participant, 'UTC')

    def update_participant_timezone(self, conversation_id: str, participant: dict, timezone: str) -> None:
        """
        Update participant's timezone and transition to next state.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                return

            if participant['role'] == 'interviewer':
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

            # Send confirmation message
            current_time = get_localized_current_time(timezone)
            system_message = (
                f"Thank you! I've set your timezone to {timezone}. "
                f"Now, please share your availability for the upcoming interviews.\n\n"
                f"Current Time: {current_time}"
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