# chatbot/message_handler.py

import logging
import random
import time
import os
from datetime import datetime
import pytz
from twilio.rest import Client
from chatbot.constants import ConversationState, AttentionFlag
from chatbot.utils import extract_slots_and_timezone, normalize_number, extract_timezone_from_number
from dotenv import load_dotenv
from .llm.llmmodel import LLMModel

load_dotenv()

logger = logging.getLogger(__name__)

class MessageHandler:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.llm_model = LLMModel()  # Instantiate LLMModel once per class

    def send_message(self, to_number: str, message: str, max_retries: int = 3, initial_retry_delay: float = 1.0) -> bool:
        from twilio.base.exceptions import TwilioRestException

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
                logger.warning(
                    f"Twilio error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: Error {e.code} - {e.msg}"
                )

                if e.code in [20003, 20426]:  # Authentication errors
                    logger.error("Authentication failed. Please check Twilio credentials.")
                    return False
                elif e.code in [21211, 21614]:  # Invalid phone number
                    logger.error(f"Invalid phone number: {to_number}")
                    return False
                elif e.code == 21617:  # Message body too long
                    logger.error("Message exceeds maximum length")
                    return False
            except Exception as e:
                retry_count += 1
                logger.warning(
                    f"Unexpected error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: {str(e)}"
                )

            if retry_count > max_retries:
                logger.error(
                    f"Failed to send message to {to_number} after {max_retries} attempts. Last error: {str(e)}"
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
            'system_message': system_message,
            'other_participant_conversation_history': other_conversation_history
        }

        if message_type == 'generate_message':
            response = self.llm_model.generate_message(**params)
        elif message_type == 'generate_conversational_message':
            response = self.llm_model.generate_conversational_message(**params)
        elif message_type == 'answer_query':
            response = self.llm_model.answer_query(**params)
        else:
            raise ValueError(f"Unknown message_type: {message_type}")

        return response

    def receive_message(self, from_number, message):
        conversation_id, participant = self.find_conversation_and_participant(from_number)
        if not conversation_id:
            logger.warning("Conversation not found for number: %s", from_number)
            return

        participant_id = participant['number']

        # Update last response time
        if 'last_response_times' not in self.scheduler.conversations[conversation_id]:
            self.scheduler.conversations[conversation_id]['last_response_times'] = {}
        self.scheduler.conversations[conversation_id]['last_response_times'][participant_id] = datetime.now(pytz.UTC)
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'last_response_times': self.scheduler.conversations[conversation_id]['last_response_times']
        })

        # Log conversation history through the scheduler
        self.scheduler.log_conversation_history(conversation_id)

        # Log incoming message via the scheduler
        self.scheduler.log_conversation(conversation_id, participant_id, "user", message, "Participant")

        # Detect intent using LLM
        intent = self.llm_model.detect_intent(
            participant_name=participant['name'],
            participant_role=participant['role'],
            meeting_duration=participant['meeting_duration'],
            role_to_contact=participant['role_to_contact_name'],
            conversation_history=" ".join(participant['conversation_history']),
            conversation_state=participant.get('state'),
            user_message=message
        )

        logger.info(f"Detected intent: {intent}")

        if "CANCELLATION_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_cancellation_request_interviewer(conversation_id, participant, message)
            elif participant['role'] == 'interviewee':
                self.handle_cancellation_request_interviewee(conversation_id, participant, message)
            return
        elif "QUERY" in intent:
            self.handle_query(conversation_id, participant, message)
            return
        elif "RESCHEDULE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_reschedule_request_interviewer(conversation_id, participant, message)
            elif participant['role'] == 'interviewee':
                self.handle_reschedule_request_interviewee(conversation_id, participant, message)
            return
        else:
            if participant['role'] == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def handle_message_from_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.conversations[conversation_id]

        # If we are awaiting slot confirmation from the interviewer
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
                # Proceed with the previously extracted slots
                temp_slots = interviewer.get('temp_slots')
                if not temp_slots:
                    system_message = "There was an error with the time slots. Please provide your availability again in a clear format."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                    return

                # Set the confirmed slots
                interviewer['slots'] = temp_slots
                interviewer['temp_slots'] = None  # Clear temporary slots
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                system_message = "Thank you for confirming your availability. We will proceed with scheduling the interviews."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                # Start scheduling with the first interviewee who awaits availability
                for interviewee in conversation['interviewees']:
                    if interviewee['state'] == ConversationState.AWAITING_AVAILABILITY.value:
                        self.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])
                        break
            else:
                # Check if new time slots are provided in the denial message
                extracted_data = extract_slots_and_timezone(
                    message,
                    interviewer['number'],
                    interviewer['conversation_history'],
                    interviewer['meeting_duration']
                )
                
                if extracted_data and extracted_data.get("time_slots"):
                    # New slots found in the message, store them temporarily and ask for confirmation
                    interviewer['temp_slots'] = extracted_data
                    self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    # Format the new slots for confirmation
                    formatted_slots = []
                    for slot in extracted_data.get("time_slots", []):
                        start_time = datetime.fromisoformat(slot['start_time'])
                        timezone = extracted_data.get('timezone', 'UTC')
                        formatted_slots.append(
                            f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                        )

                    slots_text = "\n".join(formatted_slots)
                    system_message = f"I've identified the following new time slots from your message:\n\n{slots_text}\n\nPlease confirm if these slots are correct. Reply with 'yes' to confirm or 'no' to provide different slots."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    # No new slots found in the denial message
                    interviewer['temp_slots'] = None
                    interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                    self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    system_message = (
                        "Please provide your availability in a clear format. For example:\n"
                        "- 'Available tomorrow from 2 PM to 4 PM EST'\n"
                        "- 'Available on Tuesday from 10 AM to 12 PM EST'\n"
                        "- 'Available next Monday (22nd Jan) from 3 PM to 5 PM EST'"
                    )
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                return

        # If we are awaiting more slots from the interviewer
        elif interviewer['state'] == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and extracted_data.get("time_slots"):
                # Store slots temporarily and ask for confirmation
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                # Format the slots for confirmation
                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)
                system_message = f"I've identified the following time slots from your message:\n\n{slots_text}\n\nPlease confirm if these slots are correct. Reply with 'yes' to confirm or 'no' to provide different slots."
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
                # Invalid availability format
                system_message = "Could not understand your availability. Please provide it in a clear format, for example: 'Available tomorrow from 2 PM to 4 PM EST'"
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
            # Normal interviewer message handling
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and extracted_data.get("time_slots"):
                # Store slots temporarily and ask for confirmation
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                # Format the slots for confirmation
                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)
                system_message = f"I've identified the following time slots from your message:\n\n{slots_text}\n\nPlease confirm if these slots are correct. Reply with 'yes' to confirm or 'no' to provide different slots."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
            else:
                # Invalid availability format
                system_message = (
                    "Could not understand your availability. Please provide it in a clear format. For example:\n"
                    "- 'Available tomorrow from 2 PM to 4 PM EST'\n"
                    "- 'Available on Tuesday from 10 AM to 12 PM EST'\n"
                    "- 'Available next Monday (22nd Jan) from 3 PM to 5 PM EST'"
                )
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

    def initiate_conversation_with_interviewee(self, conversation_id, interviewee_number):
        conversation = self.scheduler.conversations[conversation_id]
        interviewer = conversation['interviewer']
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)

        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        interviewee_timezone = extract_timezone_from_number(interviewee['number'])
        if interviewee_timezone and interviewee_timezone.lower() != 'unspecified':
            interviewee['timezone'] = interviewee_timezone
            self.scheduler.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })
            self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
        else:
            interviewee['state'] = ConversationState.TIMEZONE_CLARIFICATION.value
            self.scheduler.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            system_message = f"Hello {interviewee['name']}, I’m here to assist with scheduling your interview. Please let me know your timezone to proceed."
            response = self.generate_response(
                interviewee,
                interviewer,
                "Null",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_message_from_interviewee(self, conversation_id, interviewee, message):
        conversation = self.scheduler.conversations[conversation_id]
        interviewer = conversation['interviewer']

        # Timezone clarification
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
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                system_message = "Could you please specify your timezone to ensure accurate scheduling?"
                response = self.generate_response(
                    interviewee,
                    interviewer,
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
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                proposed_slot = interviewee.get('proposed_slot')
                if proposed_slot:
                    if 'archived_slots' not in conversation:
                        conversation['archived_slots'] = []
                    conversation['archived_slots'].append(proposed_slot)
                    interviewee['proposed_slot'] = None
                    self.scheduler.conversations[conversation_id]['archived_slots'] = conversation['archived_slots']
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'archived_slots': conversation['archived_slots']
                    })
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def handle_query(self, conversation_id, participant, message):
        conversation = self.scheduler.conversations.get(conversation_id)
        other_participant = None
        if participant['role'] == 'interviewer':
            other_participant = None
        else:
            other_participant = conversation.get('interviewer')

        response = self.generate_response(
            participant,
            other_participant,
            message,
            system_message="",
            conversation_state=participant.get('state'),
            message_type='answer_query'
        )

        self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_cancellation_request_interviewer(self, conversation_id, interviewer, message):
        state = interviewer.get('state')

        if state == ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value:
            interviewee_name = message.strip()
            conversation = self.scheduler.conversations.get(conversation_id)
            interviewee = next((ie for ie in conversation.get('interviewees', []) if ie['name'].lower() == interviewee_name.lower()), None)

            if not interviewee:
                system_message = f"Interviewee named '{interviewee_name}' not found. Please provide a valid interviewee name."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)

                if delete_success:
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees.$.event_id': None
                    }, {'interviewees.number': interviewee['number']})

                    cancel_message = f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled."
                    self.send_message(interviewer['number'], cancel_message)
                    self.send_message(interviewee['number'], cancel_message)

                    interviewee['state'] = ConversationState.CANCELLED.value
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    system_message = f"Successfully cancelled the meeting with {interviewee['name']}."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    system_message = "Failed to cancel the meeting due to an internal error. Please try again later."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                system_message = "No scheduled meeting found with the specified interviewee."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

            interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

        else:
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = "Please provide the name of the interviewee whose meeting you would like to cancel."
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_cancellation_request_interviewee(self, conversation_id, interviewee, message):
        event_id = interviewee.get('event_id')
        conversation = self.scheduler.conversations.get(conversation_id)
        interviewer = conversation.get('interviewer')

        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)

            if delete_success:
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees.$.event_id': None
                }, {'interviewees.number': interviewee['number']})

                conversation = self.scheduler.conversations.get(conversation_id)
                interviewer = conversation.get('interviewer')

                cancel_message = f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled."
                self.send_message(interviewee['number'], cancel_message)
                self.send_message(interviewer['number'], cancel_message)

                interviewee['state'] = ConversationState.CANCELLED.value
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                system_message = "Your interview has been successfully cancelled."
                response = self.generate_response(
                    interviewee,
                    interviewer,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
            else:
                system_message = "Failed to cancel the interview due to an internal error. Please try again later."
                response = self.generate_response(
                    interviewee,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            system_message = "No scheduled interview found to cancel."
            response = self.generate_response(
                interviewee,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_reschedule_request_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.conversations.get(conversation_id)
        scheduled_interviewees = [ie for ie in conversation['interviewees'] if ie.get('event_id')]
        if not scheduled_interviewees:
            system_message = "No scheduled meeting found to reschedule."
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
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees.$.event_id': None
                    }, {'interviewees.number': interviewee['number']})

                    interviewee['event_id'] = None
                    interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
                    interviewee['reschedule_count'] = interviewee.get('reschedule_count', 0) + 1
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })
                    self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
                else:
                    system_message = "Failed to reschedule due to an internal error. Please try again later."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                system_message = "No scheduled meeting found for that participant."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
        else:
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })
            system_message = "Multiple interviews are scheduled. Please provide the name of the interviewee whose meeting you would like to reschedule."
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_reschedule_request_interviewee(self, conversation_id, interviewee, message):
        event_id = interviewee.get('event_id')
        conversation = self.scheduler.conversations.get(conversation_id)
        interviewer = conversation.get('interviewer')

        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)
            if delete_success:
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees.$.event_id': None
                }, {'interviewees.number': interviewee['number']})

                interviewee['event_id'] = None
                interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
                interviewee['reschedule_count'] = interviewee.get('reschedule_count', 0) + 1
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                # Only call process_scheduling_for_interviewee here; do not send another message directly.
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                system_message = "Failed to reschedule due to an internal error. Please try again later."
                response = self.generate_response(
                    interviewee,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            system_message = "No scheduled meeting found to reschedule."
            response = self.generate_response(
                interviewee,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def find_conversation_and_participant(self, from_number):
        from_number_norm = normalize_number(from_number)
        for conversation_id, conversation in self.scheduler.conversations.items():
            if conversation['interviewer']['number'] == from_number_norm:
                return conversation_id, conversation['interviewer']
            for interviewee in conversation['interviewees']:
                if interviewee['number'] == from_number_norm:
                    return conversation_id, interviewee
        return None, None

    def send_reminder(self, conversation_id, participant_id):
        conversation = self.scheduler.conversations.get(conversation_id)
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

        system_message = "Hello, we noticed we haven't heard back from you. Could you please update us when you have a moment?"
        response = self.generate_response(
            participant,
            None,
            "",
            system_message,
            conversation_state=participant['state']
        )
        self.scheduler.log_conversation(conversation_id, participant_id, "system", response, "AI")
        self.send_message(participant['number'], response)
