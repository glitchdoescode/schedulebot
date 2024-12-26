# chatbot/message_handler.py
import logging
import random
import time
import os
from datetime import datetime
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
        other_participant: Optional[dict],
        user_message: str,
        system_message: str,
        conversation_state: Optional[str] = None,
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
            return "I'm sorry, something went wrong while processing your request."

    def receive_message(self, from_number: str, message: str):
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
            participant_role=participant.get('role', ''),
            meeting_duration=participant.get('meeting_duration', 60),
            role_to_contact=participant.get('role_to_contact_name', ''),
            conversation_history=" ".join(participant['conversation_history']),
            conversation_state=participant.get('state', ''),
            user_message=message
        )

        logger.info(f"Detected intent: {intent}")

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
            # Regular flow
            if participant.get('role') == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def find_conversation_and_participant(self, from_number: str, message: str):
        """
        Locate the conversation and participant by a given phone number.
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
        # Prioritize active conversations
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

    def handle_message_from_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        
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

                # Get existing available slots
                available_slots = conversation.get('available_slots', [])
                new_slots = temp_slots.get('time_slots', [])
                
                # Create set of existing slot keys
                existing_slot_keys = {self._create_slot_key(slot) for slot in available_slots}
                
                # Only add new slots that don't already exist
                filtered_new_slots = [
                    slot for slot in new_slots 
                    if self._create_slot_key(slot) not in existing_slot_keys
                ]
                
                # Add filtered new slots to available slots
                available_slots.extend(filtered_new_slots)
                
                # Update conversation with new slots
                conversation['available_slots'] = available_slots
                interviewer['temp_slots'] = None
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': conversation['available_slots'],
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

                # Initiate scheduling for interviewees with NO_SLOTS_AVAILABLE and AWAITING_AVAILABILITY
                self.initiate_scheduling_for_no_slots_available(conversation_id)
                self.initiate_scheduling_for_awaiting_availability(conversation_id)
            else:
                extracted_data = extract_slots_and_timezone(
                    message,
                    interviewer['number'],
                    interviewer.get('conversation_history', []),
                    interviewer.get('meeting_duration', 60)
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

        elif interviewer.get('state') == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
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
            # Handle initial availability from interviewer
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
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

    def initiate_conversation_with_interviewee(self, conversation_id: str, interviewee_number: str):
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
            self.process_scheduling_for_interviewee(conversation_id, interviewee_number)
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

    # def handle_message_from_interviewee(self, conversation_id: str, interviewee: dict, message: str):
    #     conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
    #     interviewer = conversation.get('interviewer')

    #     if interviewee.get('state') == ConversationState.CONFIRMATION_PENDING.value:
    #         confirmation_response = self.llm_model.detect_confirmation(
    #             participant_name=interviewee['name'],
    #             participant_role=interviewee.get('role', ''),
    #             meeting_duration=interviewee.get('meeting_duration', 60),
    #             conversation_history=" ".join(interviewee.get('conversation_history', [])),
    #             conversation_state=interviewee.get('state', ''),
    #             user_message=message
    #         )
            
    #         if confirmation_response.get('confirmed'):
    #             reserved_slots = conversation.get('reserved_slots', [])
    #             available_slots = conversation.get('available_slots', [])

    #             # After
    #             proposed_slot_key = self._create_slot_key(interviewee['proposed_slot'])
    #             reserved_slots = [slot for slot in reserved_slots if self._create_slot_key(slot) != proposed_slot_key]
    #             available_slots = [slot for slot in available_slots if self._create_slot_key(slot) != proposed_slot_key]
                                
    #             interviewee['confirmed'] = True
    #             interviewee['state'] = ConversationState.SCHEDULED.value
                
    #             # Update conversation with new states
    #             for i, ie in enumerate(conversation['interviewees']):
    #                 if ie['number'] == interviewee['number']:
    #                     conversation['interviewees'][i] = interviewee
                
    #             self.scheduler.mongodb_handler.update_conversation(conversation_id, {
    #                 'interviewees': conversation['interviewees'],
    #                 'reserved_slots': reserved_slots,
    #                 'available_slots': available_slots
    #             })
                
    #             self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])
    #         else:       
    #             # Release the reserved slot if the interviewee declines
    #             reserved_slots = conversation.get('reserved_slots', [])
    #             proposed_slot_key = self._create_slot_key(interviewee['proposed_slot'])

    #             # Find and remove the slot using key comparison
    #             reserved_slots = [
    #                 slot for slot in reserved_slots 
    #                 if self._create_slot_key(slot) != proposed_slot_key
    #             ]
    #             # Track this slot as offered to this interviewee
    #             declined_slot = interviewee['proposed_slot']
    #             interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [declined_slot]
    #             interviewee['proposed_slot'] = None
                
    #             # Update the conversation
    #             for i, ie in enumerate(conversation['interviewees']):
    #                 if ie['number'] == interviewee['number']:
    #                     conversation['interviewees'][i] = interviewee
                
    #             declined_slot_key = self._create_slot_key(declined_slot)
    #             other_interviewees = [
    #                 ie for ie in conversation['interviewees']
    #                 if (ie['number'] != interviewee['number'] and 
    #                     ie['state'] in [ConversationState.AWAITING_AVAILABILITY.value, 
    #                                 ConversationState.CONFIRMATION_PENDING.value,
    #                                 ConversationState.NO_SLOTS_AVAILABLE.value] and
    #                     declined_slot_key not in {self._create_slot_key(slot) for slot in ie.get('offered_slots', [])})
    #             ]

    #             if other_interviewees:
    #                 # Offer the declined slot to another interviewee
    #                 next_interviewee = other_interviewees[0]
    #                 next_interviewee['proposed_slot'] = declined_slot
    #                 next_interviewee['state'] = ConversationState.CONFIRMATION_PENDING.value
    #                 next_interviewee['offered_slots'] = next_interviewee.get('offered_slots', []) + [declined_slot]
                    
    #                 # Put slot back in reserved slots for the new interviewee
    #                 reserved_slots.append(declined_slot)
                    
    #                 # Update conversation state
    #                 for i, ie in enumerate(conversation['interviewees']):
    #                     if ie['number'] == next_interviewee['number']:
    #                         conversation['interviewees'][i] = next_interviewee

    #                 self.scheduler.mongodb_handler.update_conversation(conversation_id, {
    #                     'interviewees': conversation['interviewees'],
    #                     'reserved_slots': reserved_slots
    #                 })

    #                 # Send the slot to the next interviewee
    #                 timezone_str = next_interviewee.get('timezone', 'UTC')
    #                 localized_start_time = datetime.fromisoformat(declined_slot['start_time']).astimezone(
    #                     pytz.timezone(timezone_str)
    #                 ).strftime('%A, %B %d, %Y at %I:%M %p %Z')
                    
    #                 system_message = (
    #                     f"Hi {next_interviewee['name']}! 👋 We've found a potential time for your interview with "
    #                     f"Acme Corp: {localized_start_time}. Does this time work for you? 👍 Let me know! "
    #                     f"If it doesn't, we can explore other options. If we have trouble finding a suitable time, "
    #                     f"I'll contact Alice Williams at Acme Corp for help. 😊"
    #                 )
    #                 response = self.generate_response(
    #                     next_interviewee,
    #                     None,
    #                     "",
    #                     system_message,
    #                     conversation_state=next_interviewee['state']
    #                 )
    #                 self.scheduler.log_conversation(conversation_id, next_interviewee['number'], "system", response, "AI")
    #                 self.send_message(next_interviewee['number'], response)

    #             # Try to find a new slot for the current interviewee
    #             available_slots = conversation.get('available_slots', [])
    #             offered_slots = interviewee.get('offered_slots', [])

    #             # Convert to sets of slot keys for comparison
    #             offered_slot_keys = {
    #                 key for slot in offered_slots 
    #                 if (key := self._create_slot_key(slot)) is not None
    #             }
    #             reserved_slot_keys = {
    #                 key for slot in reserved_slots 
    #                 if (key := self._create_slot_key(slot)) is not None
    #             }

    #             next_slot = next(
    #                 (slot for slot in available_slots 
    #                 if self._create_slot_key(slot) not in offered_slot_keys
    #                 and self._create_slot_key(slot) not in reserved_slot_keys),
    #                 None
    #             )
    #             if next_slot:
    #                 # Offer new slot to current interviewee
    #                 interviewee['proposed_slot'] = next_slot
    #                 interviewee['state'] = ConversationState.CONFIRMATION_PENDING.value
    #                 interviewee['offered_slots'] = offered_slots + [next_slot]
    #                 reserved_slots.append(next_slot)
                    
    #                 for i, ie in enumerate(conversation['interviewees']):
    #                     if ie['number'] == interviewee['number']:
    #                         conversation['interviewees'][i] = interviewee

    #                 self.scheduler.mongodb_handler.update_conversation(conversation_id, {
    #                     'interviewees': conversation['interviewees'],
    #                     'reserved_slots': reserved_slots
    #                 })

    #                 # Send new proposed slot to current interviewee
    #                 timezone_str = interviewee.get('timezone', 'UTC')
    #                 localized_start_time = datetime.fromisoformat(next_slot['start_time']).astimezone(
    #                     pytz.timezone(timezone_str)
    #                 ).strftime('%A, %B %d, %Y at %I:%M %p %Z')
                    
    #                 system_message = (
    #                     f"No problem! Let's try another time. How about: {localized_start_time}? "
    #                     f"Let me know if this works better for you. 👍"
    #                 )
    #                 response = self.generate_response(
    #                     interviewee,
    #                     None,
    #                     message,
    #                     system_message,
    #                     conversation_state=interviewee['state']
    #                 )
    #                 self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
    #                 self.send_message(interviewee['number'], response)
    #             else:
    #                 # If all slots have been offered to all interviewees, then ask interviewer for more
    #                 all_slots_offered = True
    #                 # Convert available slots to key set once, outside the loop
    #                 available_slot_keys = {
    #                     key for slot in available_slots 
    #                     if (key := self._create_slot_key(slot)) is not None
    #                 }

    #                 for ie in conversation['interviewees']:
    #                     if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]:
    #                         # Convert offered slots to keys for comparison
    #                         ie_offered_slot_keys = {
    #                             key for slot in offered_slots 
    #                             if (key := self._create_slot_key(slot)) is not None
    #                         }
                                                        
    #                         # Compare using slot keys
    #                         if not available_slot_keys.issubset(ie_offered_slot_keys):
    #                             all_slots_offered = False
    #                             break

    #                 if all_slots_offered:
    #                     # Ask interviewer for more slots
    #                     interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
    #                     self.scheduler.mongodb_handler.update_conversation(conversation_id, {
    #                         'interviewer': interviewer
    #                     })

    #                     # Notify interviewer
    #                     timezone_str = interviewer.get('timezone', 'UTC')
    #                     current_time = get_localized_current_time(timezone_str)

    #                     # Get list of unscheduled interviewees
    #                     unscheduled = [ie['name'] for ie in conversation['interviewees'] 
    #                                 if ie['state'] not in [ConversationState.SCHEDULED.value, 
    #                                                     ConversationState.CANCELLED.value]]

    #                     system_message = (
    #                         f"All available slots have been tried with the interviewees. The following "
    #                         f"interviewees still need to be scheduled: {', '.join(unscheduled)}. "
    #                         f"Could you please provide more availability?\n\nCurrent Time: {current_time}"
    #                     )
    #                     response = self.generate_response(
    #                         interviewer,
    #                         None,
    #                         "",
    #                         system_message,
    #                         conversation_state=interviewer['state']
    #                     )
    #                     self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
    #                     self.send_message(interviewer['number'], response)
    #                 else:
    #                     # Continue with slot recycling for remaining interviewees
    #                     self.process_remaining_interviewees(conversation_id)

    def handle_message_from_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        """
        Handle messages from interviewees, focusing on slot confirmation and denial logic.
        
        Args:
            conversation_id (str): The unique identifier for the conversation
            interviewee (dict): The interviewee's information and state
            message (str): The message received from the interviewee
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        interviewer = conversation.get('interviewer')

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
                self._handle_slot_denial(conversation_id, interviewee, conversation)

    def _handle_slot_acceptance(self, conversation_id: str, interviewee: dict, conversation: dict):
        """
        Handle slot acceptance by an interviewee.
        
        Args:
            conversation_id (str): The unique identifier for the conversation
            interviewee (dict): The interviewee who accepted the slot
            conversation (dict): The current conversation state
        """
        # Remove accepted slot from available and reserved pools
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])
        
        accepted_slot_key = self._create_slot_key(interviewee['proposed_slot'])
        reserved_slots = [slot for slot in reserved_slots if self._create_slot_key(slot) != accepted_slot_key]
        available_slots = [slot for slot in available_slots if self._create_slot_key(slot) != accepted_slot_key]
        
        # Update interviewee state
        interviewee['confirmed'] = True
        interviewee['state'] = ConversationState.SCHEDULED.value
        
        # Update conversation with new states
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee
        
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': available_slots
        })
        
        self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def _handle_slot_denial(self, conversation_id: str, interviewee: dict, conversation: dict):
        """
        Handle slot denial with proper recycling of slots and offering to other interviewees.
        """
        # Get current state
        reserved_slots = conversation.get('reserved_slots', [])
        denied_slot = interviewee['proposed_slot']
        available_slots = conversation.get('available_slots', [])

        if denied_slot:
            # Track this slot as offered to this interviewee
            interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [denied_slot]
            interviewee['proposed_slot'] = None
            
            # Mark as NO_SLOTS_AVAILABLE temporarily to avoid immediate reprocessing
            interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value

            # Remove from reserved slots
            reserved_slots = [
                slot for slot in reserved_slots 
                if self._create_slot_key(slot) != self._create_slot_key(denied_slot)
            ]

            # Important: Put the denied slot back in available slots if not already there
            denied_slot_key = self._create_slot_key(denied_slot)
            if not any(self._create_slot_key(slot) == denied_slot_key for slot in available_slots):
                available_slots.append(denied_slot)

            # Find all other interviewees who haven't been offered this slot yet
            other_interviewees = [
                ie for ie in conversation['interviewees']
                if (ie['number'] != interviewee['number'] and 
                    ie['state'] in [
                        ConversationState.AWAITING_AVAILABILITY.value,
                        ConversationState.NO_SLOTS_AVAILABLE.value
                    ] and
                    denied_slot_key not in {
                        self._create_slot_key(slot)
                        for slot in ie.get('offered_slots', [])
                    })
            ]

            # Update conversation state
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee['number']:
                    conversation['interviewees'][i] = interviewee

            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees'],
                'reserved_slots': reserved_slots,
                'available_slots': available_slots
            })

            # If there are other interviewees who haven't seen this slot
            if other_interviewees:
                # Process the next interviewee
                next_interviewee = other_interviewees[0]
                self.process_scheduling_for_interviewee(
                    conversation_id,
                    next_interviewee['number']
                )
            else:
                # Try to find more slots for the current interviewee
                self._find_next_slot_for_interviewee(
                    conversation_id,
                    interviewee,
                    conversation,
                    reserved_slots
                )

    def _offer_slot_to_interviewee(
        self, 
        conversation_id: str, 
        interviewee: dict, 
        slot: dict, 
        reserved_slots: list,
        conversation: dict
    ):
        """
        Offer a slot to an interviewee and update necessary state.
        
        Args:
            conversation_id (str): The unique identifier for the conversation
            interviewee (dict): The interviewee to offer the slot to
            slot (dict): The slot to offer
            reserved_slots (list): Current reserved slots
            conversation (dict): The current conversation state
        """
        interviewee['proposed_slot'] = slot
        interviewee['state'] = ConversationState.CONFIRMATION_PENDING.value
        interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [slot]
        
        # Update reserved slots
        reserved_slots.append(slot)
        
        # Update conversation state
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee
        
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots
        })
        
        # Send slot proposal message
        timezone_str = interviewee.get('timezone', 'UTC')
        localized_start_time = datetime.fromisoformat(slot['start_time']).astimezone(
            pytz.timezone(timezone_str)
        ).strftime('%A, %B %d, %Y at %I:%M %p %Z')
        
        system_message = (
            f"Hi {interviewee['name']}! 👋 We've found a potential time for your interview with "
            f"Acme Corp: {localized_start_time}. Does this time work for you? 👍 Let me know! "
            f"If it doesn't, we can explore other options. If we have trouble finding a suitable time, "
            f"I'll contact Alice Williams at Acme Corp for help. 😊"
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

    def _find_next_slot_for_interviewee(
        self, 
        conversation_id: str, 
        interviewee: dict,
        conversation: dict,
        reserved_slots: list
    ):
        """
        Find and offer the next available slot for an interviewee.
        
        Args:
            conversation_id (str): The unique identifier for the conversation
            interviewee (dict): The interviewee to find a slot for
            conversation (dict): The current conversation state
            reserved_slots (list): Current reserved slots
        """
        available_slots = conversation.get('available_slots', [])
        offered_slots = interviewee.get('offered_slots', [])
        
        # Find next available slot not already offered or reserved
        offered_slot_keys = {
            key for slot in offered_slots 
            if (key := self._create_slot_key(slot)) is not None
        }
        reserved_slot_keys = {
            key for slot in reserved_slots 
            if (key := self._create_slot_key(slot)) is not None
        }
        next_slot = next(
            (slot for slot in available_slots 
            if self._create_slot_key(slot) not in offered_slot_keys
            and self._create_slot_key(slot) not in reserved_slot_keys),
            None
        )
        
        if next_slot:
            self._offer_slot_to_interviewee(
                conversation_id,
                interviewee,
                next_slot,
                reserved_slots,
                conversation
            )
        else:
            self._handle_no_slots_available(conversation_id, interviewee, conversation)

    def _handle_no_slots_available(self, conversation_id: str, interviewee: dict, conversation: dict):
        """
        Handle the case when no more slots are available for an interviewee.
        
        Args:
            conversation_id (str): The unique identifier for the conversation
            interviewee (dict): The interviewee with no available slots
            conversation (dict): The current conversation state
        """
        interviewer = conversation.get('interviewer')
        
        # Update interviewee state
        interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee
        
        # Update interviewer state
        interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
        
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'interviewer': interviewer
        })
        
        # Notify interviewer
        timezone_str = interviewer.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)
        
        # Get list of unscheduled interviewees
        unscheduled = [
            ie['name'] for ie in conversation['interviewees']
            if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value
        ]
        
        system_message = (
            f"All available slots have been offered to interviewees. The following "
            f"interviewees could not be scheduled: {', '.join(unscheduled)}. Could you "
            f"please provide more availability?\n\nCurrent Time: {current_time}"
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

    def process_remaining_interviewees(self, conversation_id: str):
        """
        Process scheduling for remaining interviewees sequentially.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            return

        # Check for any pending confirmations
        pending_interviewees = [
            ie for ie in conversation['interviewees']
            if ie['state'] == ConversationState.CONFIRMATION_PENDING.value
        ]
        
        if pending_interviewees:
            # Wait for pending responses
            return

        # Process interviewees who are awaiting availability
        awaiting_interviewees = [
            ie for ie in conversation['interviewees']
            if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value
        ]

        if awaiting_interviewees:
            # Process one interviewee at a time
            self.process_scheduling_for_interviewee(conversation_id, awaiting_interviewees[0]['number'])
        else:
            # Check if any interviewees need more slots
            no_slots_interviewees = [
                ie for ie in conversation['interviewees']
                if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value
            ]
            
            if no_slots_interviewees:
                self._request_more_slots(conversation_id, no_slots_interviewees, conversation)

    def _request_more_slots(self, conversation_id: str, unscheduled_interviewees: list, conversation: dict):
        """
        Request more slots from the interviewer when needed.
        """
        interviewer = conversation.get('interviewer')
        if not interviewer:
            return

        interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewer': interviewer
        })

        # Notify interviewer
        timezone_str = interviewer.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        unscheduled_names = [ie['name'] for ie in unscheduled_interviewees]
        system_message = (
            f"All available slots have been offered. The following "
            f"interviewees still need to be scheduled: {', '.join(unscheduled_names)}. "
            f"Could you please provide more availability?\n\nCurrent Time: {current_time}"
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

    def handle_query(self, conversation_id: str, participant: dict, message: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        other_participant = None
        if participant.get('role') != 'interviewer':
            other_participant = conversation.get('interviewer')

        # Get localized current time
        timezone_str = participant.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        response = self.generate_response(
            participant,
            other_participant,
            message,
            system_message=f"Your query has been received.\n\nCurrent Time: {current_time}",
            conversation_state=participant.get('state'),
            message_type='answer_query'
        )

        self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_cancellation_request_interviewer(self, conversation_id: str, interviewer: dict, message: str):
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
            if self.scheduler.is_conversation_complete(conversation):
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

    def handle_cancellation_request_interviewee(self, conversation_id: str, interviewee: dict, message: str):
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
                        response = self.generate_response(interviewee_obj, None, message, system_message)
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
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
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

    def handle_reschedule_request_interviewer(self, conversation_id: str, interviewer: dict, message: str):
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

                    self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
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

    def handle_reschedule_request_interviewee(self, conversation_id: str, interviewee: dict, message: str):
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
                self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
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
        if self.scheduler.is_conversation_complete(conversation):
            self.complete_conversation(conversation_id)

    def send_reminder(self, conversation_id: str, participant_id: str):
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
            conversation_state=participant.get('state')
        )
        self.scheduler.log_conversation(conversation_id, participant_id, "system", response, "AI")
        self.send_message(participant['number'], response)

    def update_participant_timezone(self, conversation_id: str, participant: dict, timezone: str) -> None:
        """
        Update participant's timezone and transition to next state.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
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

    def initiate_scheduling_for_no_slots_available(self, conversation_id: str):
        """
        Initiate scheduling for interviewees whose state is NO_SLOTS_AVAILABLE after new slots are added.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiating scheduling.")
            return

        no_slots_interviewees = [ie for ie in conversation['interviewees'] if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value]
        if not no_slots_interviewees:
            logger.info(f"No interviewees with NO_SLOTS_AVAILABLE in conversation {conversation_id}.")
            return

        for interviewee in no_slots_interviewees:
            self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def initiate_scheduling_for_awaiting_availability(self, conversation_id: str):
        """
        Initiate scheduling for interviewees whose state is AWAITING_AVAILABILITY.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiating scheduling.")
            return

        awaiting_availability = [ie for ie in conversation['interviewees'] if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value]
        if not awaiting_availability:
            logger.info(f"No interviewees with AWAITING_AVAILABILITY in conversation {conversation_id}.")
            return

        for interviewee in awaiting_availability:
            self.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])

    def complete_conversation(self, conversation_id: str):
        """
        Mark the conversation as complete and perform any necessary cleanup.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found for completion.")
                return

            conversation['status'] = 'completed'
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'status': 'completed'
            })

            # Notify interviewer
            interviewer = conversation['interviewer']
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            system_message = f"All interviews have been scheduled or cancelled. Thank you!\n\nCurrent Time: {current_time}"
            response = self.generate_response(
                interviewer,
                None,
                "",
                system_message,
                conversation_state=ConversationState.COMPLETED.value
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

        except Exception as e:
            logger.error(f"Error completing conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())

    def process_scheduling_for_interviewee(self, conversation_id: str, interviewee_number: str):
        """
        Process scheduling by offering one slot at a time to interviewees.
        Ensures proper sequencing of slot offers and responses.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for scheduling.")
            return

        # Check if any other interviewee is in CONFIRMATION_PENDING state
        pending_interviewees = [
            ie for ie in conversation['interviewees']
            if ie['state'] == ConversationState.CONFIRMATION_PENDING.value
            and ie['number'] != interviewee_number
        ]
        
        if pending_interviewees:
            # Wait for pending responses before offering new slots
            interviewee = next((ie for ie in conversation['interviewees'] 
                            if ie['number'] == interviewee_number), None)
            if interviewee:
                interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee_number:
                        conversation['interviewees'][i] = interviewee
                
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
            return

        interviewee = next((ie for ie in conversation['interviewees'] 
                        if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        available_slots = conversation.get('available_slots', [])
        offered_slots = interviewee.get('offered_slots', [])
        reserved_slots = conversation.get('reserved_slots', [])

        # Find next available slot
        offered_slot_keys = {
            key for slot in offered_slots 
            if (key := self._create_slot_key(slot)) is not None
        }
        reserved_slot_keys = {
            key for slot in reserved_slots 
            if (key := self._create_slot_key(slot)) is not None
        }
        next_slot = next(
            (slot for slot in available_slots 
            if self._create_slot_key(slot) not in offered_slot_keys
            and self._create_slot_key(slot) not in reserved_slot_keys),
            None
        )

        if next_slot:
            self._offer_slot_to_interviewee(
                conversation_id,
                interviewee,
                next_slot,
                reserved_slots,
                conversation
            )
        else:
            self._handle_no_slots_available(conversation_id, interviewee, conversation)
            
    def _create_slot_key(self, slot):
        """
        Create a unique, hashable key from a slot dictionary.
        Args:
            slot (dict): Slot dictionary containing start_time
        Returns:
            str: A unique hashable key, or None if slot is invalid
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