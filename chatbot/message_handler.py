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
            logger.error("Missing Twilio credentials. Check environment variables.")
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
                    logger.error("Authentication failed. Check Twilio credentials.")
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
                    f"Failed to send message to {to_number} after {max_retries} attempts. "
                    f"Last error: {str(last_exception)}"
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

        now_utc = datetime.now(pytz.UTC)
        self.scheduler.mongodb_handler.update_conversation(
            conversation_id,
            {f'last_response_times.{participant["number"]}': now_utc.isoformat()}
        )

        self.scheduler.log_conversation_history(conversation_id)
        self.scheduler.log_conversation(conversation_id, participant['number'], "user", message, "Participant")

        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)

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
            if participant.get('role') == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def find_conversation_and_participant(self, from_number: str, message: str):
        from_number_norm = normalize_number(from_number)
        conversations = self.scheduler.mongodb_handler.find_conversations_by_number(from_number_norm)

        if not conversations:
            return None, None, None

        if len(conversations) == 1:
            conversation = conversations[0]
            participant = (conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm
                           else next((ie for ie in conversation['interviewees']
                                      if ie['number'] == from_number_norm), None))
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        active_conversations = [c for c in conversations if c['status'] == 'active']
        if active_conversations:
            conversation = sorted(active_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm
                           else next((ie for ie in conversation['interviewees']
                                      if ie['number'] == from_number_norm), None))
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        queued_conversations = [c for c in conversations if c['status'] == 'queued']
        if queued_conversations:
            conversation = sorted(queued_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm
                           else next((ie for ie in conversation['interviewees']
                                      if ie['number'] == from_number_norm), None))
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

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
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"Error with the time slots. Please provide availability again.\n\n"
                        f"Current Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                    return

                available_slots = conversation.get('available_slots', [])
                new_slots = temp_slots.get('time_slots', [])
                existing_slot_keys = {self._create_slot_key(slot) for slot in available_slots}
                filtered_new_slots = [slot for slot in new_slots
                                      if self._create_slot_key(slot) not in existing_slot_keys]
                available_slots.extend(filtered_new_slots)

                conversation['available_slots'] = available_slots
                interviewer['temp_slots'] = None
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': available_slots,
                    'interviewer': interviewer
                })

                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"Thank you! We'll proceed with scheduling the interviews using these slots.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                self.initiate_scheduling_for_no_slots_available(conversation_id)
                self.initiate_scheduling_for_awaiting_availability(conversation_id)

            else:
                # Interviewer refused or provided new slots inline
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
                        tz = extracted_data.get('timezone', 'UTC')
                        formatted_slots.append(
                            f"- {start_time.astimezone(pytz.timezone(tz)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                        )
                    slots_text = "\n".join(formatted_slots)
                    current_time = get_localized_current_time(tz)

                    system_message = (
                        f"New time slots identified:\n\n{slots_text}\n\n"
                        f"Reply 'yes' to confirm or 'no' to provide different slots.\n\n"
                        f"Current Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    interviewer['temp_slots'] = None
                    interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"Please share your availability again in a clear format.\n\n"
                        f"Current Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

        elif interviewer.get('state') == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            # They were asked for additional slots
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
            )
            if extracted_data and 'time_slots' in extracted_data:
                available_slots = conversation.get('available_slots', [])
                new_slots = extracted_data.get('time_slots', [])
                existing_keys = {self._create_slot_key(slot) for slot in available_slots}
                filtered_new_slots = [
                    slot for slot in new_slots
                    if self._create_slot_key(slot) not in existing_keys
                ]
                available_slots.extend(filtered_new_slots)

                # REFACTOR: Use numeric counter to track more slot requests
                conversation['more_slots_requests'] = conversation.get('more_slots_requests', 0)
                # We already asked once to get here, so do not increment now. We increment upon request.
                # If you'd prefer a different approach, feel free to move the increment logic.

                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': available_slots,
                    'interviewer': interviewer
                })

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

                # Attempt to schedule for the leftover interviewees
                if unscheduled:
                    self.process_scheduling_for_interviewee(conversation_id, unscheduled[0]['number'])
                else:
                    self.complete_conversation(conversation_id)
            else:
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"No valid slots were detected. Please try again.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

        else:
            # Normal scenario: interviewer shares slots the first time
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
                    tz = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(tz)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )
                slots_text = "\n".join(formatted_slots)
                current_time = get_localized_current_time(tz)

                system_message = (
                    f"Identified these time slots:\n\n{slots_text}\n\n"
                    f"Reply 'yes' to confirm or 'no' to provide different slots.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
            else:
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"Could not understand your availability. Please provide it in a clear format.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

    def handle_message_from_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
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
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])
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
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])
        slot_denials = conversation.get('slot_denials', {})

        # --- FIX: Convert any stored lists back to sets ---
        for k, val in slot_denials.items():
            if isinstance(val, list):
                slot_denials[k] = set(val)
        # ---------------------------------------------------

        denied_slot = interviewee['proposed_slot']
        denied_slot_key = self._create_slot_key(denied_slot)

        # Mark as offered so we don't re-offer it to them
        interviewee['offered_slots'] = interviewee.get('offered_slots', []) + [denied_slot]
        interviewee['proposed_slot'] = None

        # Remove it from their reservation
        reserved_slots = [
            slot for slot in reserved_slots
            if self._create_slot_key(slot) != denied_slot_key
        ]

        # Count this denial in our in-memory set
        if denied_slot_key not in slot_denials:
            slot_denials[denied_slot_key] = set()
        slot_denials[denied_slot_key].add(interviewee['number'])

        # Check how many interviewees remain unscheduled
        unscheduled_ies = [
            ie for ie in conversation['interviewees']
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]
        ]
        all_unscheduled_nums = {ie['number'] for ie in unscheduled_ies}

        # If all unscheduled interviewees have denied this slot, remove it globally
        if slot_denials[denied_slot_key].issuperset(all_unscheduled_nums):
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

        # --- FIX: Store back as lists before saving to MongoDB ---
        conversation['slot_denials'] = {
            k: list(v) for k, v in slot_denials.items()
        }
        # ---------------------------------------------------------

        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        # Check if they still have untried slots
        untried_slots = self._get_untried_slots_for_interviewee(interviewee, available_slots, reserved_slots)
        if untried_slots:
            interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
        else:
            interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
            logger.info(f"Interviewee {interviewee['name']} moved to NO_SLOTS_AVAILABLE after denying all offered slots.")

        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': conversation['available_slots'],
            'slot_denials': conversation['slot_denials']
        })

        # Continue the scheduling flow
        self.process_remaining_interviewees(conversation_id)


    def process_remaining_interviewees(self, conversation_id: str):
        """
        This method is called after an interviewee denies or completes scheduling, 
        or after we add new slots. We want to keep scheduling going in parallel.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            return

        # Instead of returning if we find one CONFIRMATION_PENDING, we skip it 
        # and let others proceed in parallel.

        # 1) For interviewees in AWAITING_AVAILABILITY, we try to schedule them.
        changed_something = True
        while changed_something:
            changed_something = False
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                return

            # Offer next slot to every interviewee in AWAITING_AVAILABILITY
            awaiting = [ie for ie in conversation['interviewees']
                        if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value]
            for interviewee in awaiting:
                self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
                changed_something = True

            # 2) For those in NO_SLOTS_AVAILABLE, see if a newly freed slot might now be available
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
                    conversation_id, {'interviewees': conversation['interviewees']}
                )
                changed_something = True

        # After we tried reassigning states, let's see if anyone is left unscheduled
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        unscheduled = [
            ie for ie in conversation['interviewees']
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]
        ]

        # If at least one is CONFIRMATION_PENDING, we do nothing; we wait for them.
        pending = [ie for ie in unscheduled if ie['state'] == ConversationState.CONFIRMATION_PENDING.value]
        if pending:
            logger.info("Some interviewees are in CONFIRMATION_PENDING; scheduling for others has continued in parallel.")
            return

        # Anyone else is either NO_SLOTS_AVAILABLE or some other unscheduled state 
        # if we can't proceed, we ask for more slots or finalize
        if unscheduled:
            # Check how many times we've asked for more slots
            requests_count = conversation.get('more_slots_requests', 0)
            if requests_count >= 2:
                # If we already asked 2+ times, finalize
                logger.info("Reached maximum number of requests for more slots. Finalizing conversation.")
                self.complete_conversation(conversation_id)
            else:
                # Request more slots
                self._request_more_slots(conversation_id, unscheduled, conversation)
        else:
            # All scheduled or cancelled -> complete
            self.complete_conversation(conversation_id)

    def process_scheduling_for_interviewee(self, conversation_id: str, interviewee_number: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            return

        interviewee = next((ie for ie in conversation['interviewees']
                            if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

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

            timezone_str = interviewee.get('timezone', 'UTC')
            localized_start_time = datetime.fromisoformat(next_slot['start_time']).astimezone(
                pytz.timezone(timezone_str)
            ).strftime('%A, %B %d, %Y at %I:%M %p %Z')

            system_message = (
                f"Hi {interviewee['name']}! We've found a potential time for your interview: {localized_start_time}. "
                f"Does this time work for you?"
            )
            response = self.generate_response(
                interviewee,
                None,
                "",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee_number, "system", response, "AI")
            self.send_message(interviewee['number'], response)
        else:
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
        offered_keys = {self._create_slot_key(slot) for slot in interviewee.get('offered_slots', [])}
        reserved_keys = {self._create_slot_key(slot) for slot in reserved_slots}
        return [
            slot for slot in available_slots
            if (self._create_slot_key(slot) not in offered_keys) and
               (self._create_slot_key(slot) not in reserved_keys)
        ]

    def _request_more_slots(self, conversation_id: str, unscheduled: list, conversation: dict):
        interviewer = conversation.get('interviewer')
        if not interviewer:
            return

        interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value

        # REFACTOR: increment the number of times we've asked for more slots
        conversation['more_slots_requests'] = conversation.get('more_slots_requests', 0) + 1
        # REFACTOR: store the timestamp
        conversation['last_more_slots_request_time'] = datetime.now(pytz.UTC).isoformat()

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewer': interviewer,
            'more_slots_requests': conversation['more_slots_requests'],
            'last_more_slots_request_time': conversation['last_more_slots_request_time']
        })

        timezone_str = interviewer.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)
        unscheduled_names = [ie['name'] for ie in unscheduled]

        system_message = (
            f"All current slots have been tried. The following interviewees are still unscheduled: "
            f"{', '.join(unscheduled_names)}. Could you please provide more availability?\n\n"
            f"Current Time: {current_time}"
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
        Mark conversation as completed & notify the interviewer. 
        This is a simplified version that defers final conversation closure to the InterviewScheduler's method.
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found.")
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
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            if unscheduled:
                note = f"Some interviewees could not be scheduled: {', '.join(unscheduled)}."
            else:
                note = "All interviews have been successfully scheduled."

            system_message = (
                f"All scheduling steps have been completed. {note} "
                f"Thank you!\n\nCurrent Time: {current_time}"
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

            # Let the central scheduler finalize
            self.scheduler.complete_conversation(conversation_id)

        except Exception as e:
            logger.error(f"Error completing conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())

    def send_reminder(self, conversation_id: str, participant_id: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for sending reminder.")
            return

        if participant_id == 'interviewer':
            participant = conversation['interviewer']
        else:
            participant = next((ie for ie in conversation['interviewees']
                                if ie['number'] == participant_id), None)

        if not participant:
            logger.error(f"Participant {participant_id} not found in conversation {conversation_id}.")
            return

        timezone_str = participant.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)
        system_message = (
            f"Hello, we haven't heard from you. Could you please provide an update?\n\n"
            f"Current Time: {current_time}"
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

            current_time = get_localized_current_time(timezone)
            system_message = (
                f"Thanks! I've set your timezone to {timezone}. "
                f"Please share your availability for the upcoming interviews.\n\n"
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

    def initiate_conversation_with_interviewee(self, conversation_id: str, interviewee_number: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            return

        interviewee = next((ie for ie in conversation['interviewees']
                            if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        # Attempt auto-detect
        interviewee_timezone = extract_timezone_from_number(interviewee['number'])
        if interviewee_timezone and interviewee_timezone.lower() != 'unspecified':
            interviewee['timezone'] = interviewee_timezone
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

            timezone_str = 'UTC'
            current_time = get_localized_current_time(timezone_str)
            system_message = (
                f"Hello {interviewee['name']}, please let me know your timezone to proceed.\n\n"
                f"Current Time: {current_time}"
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
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
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
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
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
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        other_participant = None
        if participant.get('role') != 'interviewer':
            other_participant = conversation.get('interviewer')

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
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee['number']:
                            ie['event_id'] = None
                            ie['state'] = ConversationState.CANCELLED.value
                            conversation['interviewees'][i] = ie
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    cancel_message = (
                        f"The meeting between {interviewer['name']} and {interviewee['name']} "
                        f"has been cancelled.\n\nCurrent Time: {current_time}"
                    )
                    self.send_message(interviewer['number'], cancel_message)
                    self.send_message(interviewee['number'], cancel_message)

                    system_message = f"Cancelled the meeting with {interviewee['name']}.\n\nCurrent Time: {current_time}"
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"Failed to cancel due to an internal error.\n\nCurrent Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
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

            if self.scheduler.is_conversation_complete(conversation):
                self.complete_conversation(conversation_id)

        else:
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

                        timezone_str = interviewer.get('timezone', 'UTC')
                        current_time = get_localized_current_time(timezone_str)
                        cancel_message = (
                            f"The meeting between {interviewer['name']} and {interviewee_obj['name']} "
                            f"has been cancelled.\n\nCurrent Time: {current_time}"
                        )
                        self.send_message(interviewer['number'], cancel_message)
                        self.send_message(interviewee_obj['number'], cancel_message)

                        system_message = (
                            f"Cancelled the meeting with {interviewee_obj['name']}.\n\n"
                            f"Current Time: {current_time}"
                        )
                        response = self.generate_response(interviewee_obj, None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)
                    else:
                        timezone_str = interviewer.get('timezone', 'UTC')
                        current_time = get_localized_current_time(timezone_str)
                        system_message = (
                            f"Failed to cancel due to an internal error.\n\nCurrent Time: {current_time}"
                        )
                        response = self.generate_response(interviewee_obj, None, message, system_message)
                        self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                        self.send_message(interviewee_obj['number'], response)
                else:
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"No scheduled meeting found with that interviewee.\n\nCurrent Time: {current_time}"
                    )
                    response = self.generate_response(interviewee_obj, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, interviewee_obj['number'], "system", response, "AI")
                    self.send_message(interviewee_obj['number'], response)
            else:
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"No interviewee found with that name. Please provide a valid interviewee's name to cancel.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            timezone_str = interviewee.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            interviewee['state'] = ConversationState.AWAITING_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            system_message = (
                f"Please provide the name of the interviewee whose interview you wish to cancel.\n\n"
                f"Current Time: {current_time}"
            )
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_reschedule_request_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        scheduled = [ie for ie in conversation['interviewees'] if ie.get('event_id')]
        if not scheduled:
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)
            system_message = f"No scheduled meeting found to reschedule.\n\nCurrent Time: {current_time}"
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        if len(scheduled) == 1:
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

                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"Rescheduling the meeting with {target_ie['name']}.\n\n"
                        f"Current Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

                    self.process_scheduling_for_interviewee(conversation_id, target_ie['number'])
                else:
                    timezone_str = interviewer.get('timezone', 'UTC')
                    current_time = get_localized_current_time(timezone_str)
                    system_message = (
                        f"Failed to reschedule due to an internal error. Please try again later.\n\n"
                        f"Current Time: {current_time}"
                    )
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = f"No scheduled meeting found for that participant.\n\nCurrent Time: {current_time}"
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
        else:
            timezone_str = interviewer.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)

            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = (
                f"Multiple scheduled interviews found. Please provide the interviewee's name to reschedule.\n\n"
                f"Current Time: {current_time}"
            )
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_reschedule_request_interviewee(self, conversation_id: str, interviewee: dict, message: str):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        event_id = interviewee.get('event_id')
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
                timezone_str = interviewee.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"Failed to reschedule due to an internal error. Please try again later.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewee, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            timezone_str = interviewee.get('timezone', 'UTC')
            current_time = get_localized_current_time(timezone_str)
            system_message = (
                f"No scheduled meeting found to reschedule.\n\n"
                f"Current Time: {current_time}"
            )
            response = self.generate_response(interviewee, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

        if self.scheduler.is_conversation_complete(conversation):
            self.complete_conversation(conversation_id)

    def _create_slot_key(self, slot):
        if not slot:
            logger.error("Invalid slot: slot is None or empty")
            return None
        if 'start_time' not in slot:
            logger.error(f"Invalid slot format: missing start_time in slot {slot}")
            return None

        key = f"{slot['start_time']}"
        logger.debug(f"Created slot key: {key} for slot: {slot}")
        return key
