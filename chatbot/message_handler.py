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
        """
        Send a WhatsApp message via Twilio with retry logic.
        """
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

            # Exponential backoff + small jitter
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
        Use the LLM model to generate system responses or answer queries.
        """
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
        """
        Primary entry point for incoming messages from Twilio.
        """
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
            # Regular scheduling flow
            if participant.get('role') == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def find_conversation_and_participant(self, from_number: str, message: str):
        """
        Identify the correct conversation and participant from the DB by phone number.
        """
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

        # If multiple conversations are found
        active_conversations = [c for c in conversations if c['status'] == 'active']
        if active_conversations:
            # Take the latest active conversation
            conversation = sorted(active_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm
                           else next((ie for ie in conversation['interviewees']
                                      if ie['number'] == from_number_norm), None))
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        # If no active conversation, check queued
        queued_conversations = [c for c in conversations if c['status'] == 'queued']
        if queued_conversations:
            conversation = sorted(queued_conversations, key=lambda x: x['created_at'], reverse=True)[0]
            participant = (conversation['interviewer'] if conversation['interviewer']['number'] == from_number_norm
                           else next((ie for ie in conversation['interviewees']
                                      if ie['number'] == from_number_norm), None))
            return conversation['conversation_id'], participant, conversation['interviewer']['number']

        return None, None, None

    def handle_message_from_interviewer(self, conversation_id: str, interviewer: dict, message: str):
        """
        Handle messages from the interviewer regarding availability slots.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)

        if interviewer.get('state') == ConversationState.AWAITING_SLOT_CONFIRMATION.value:
            # They previously sent slots and are awaiting confirmation
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewer['name'],
                participant_role=interviewer.get('role', ''),
                meeting_duration=interviewer.get('meeting_duration', 60),
                conversation_history=" ".join(interviewer.get('conversation_history', [])),
                conversation_state=interviewer.get('state', ''),
                user_message=message
            )

            if confirmation_response.get('confirmed'):
                # Confirm the newly provided slots
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
                # Avoid duplicates
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

                # Thank the interviewer
                timezone_str = interviewer.get('timezone', 'UTC')
                current_time = get_localized_current_time(timezone_str)
                system_message = (
                    f"Thank you! We'll proceed with scheduling the interviews using these slots.\n\n"
                    f"Current Time: {current_time}"
                )
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                # Try scheduling for interviewees that had no slots
                self.initiate_scheduling_for_no_slots_available(conversation_id)
                # And also for those awaiting availability
                self.initiate_scheduling_for_awaiting_availability(conversation_id)

            else:
                # Interviewer said "no" or is providing new slots
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

                    # List them out for confirmation
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
                    # They said no but didn't provide new slots
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
            # The interviewer was asked for additional slots
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer.get('conversation_history', []),
                interviewer.get('meeting_duration', 60)
            )
            if extracted_data and 'time_slots' in extracted_data:
                # Merge new slots
                available_slots = conversation.get('available_slots', [])
                new_slots = extracted_data.get('time_slots', [])
                existing_keys = {self._create_slot_key(slot) for slot in available_slots}
                filtered_new_slots = [
                    slot for slot in new_slots
                    if self._create_slot_key(slot) not in existing_keys
                ]
                available_slots.extend(filtered_new_slots)

                conversation['available_slots'] = available_slots
                conversation['final_scheduling_round'] = True
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value

                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': available_slots,
                    'interviewer': interviewer,
                    'final_scheduling_round': True
                })

                # Reset leftover interviewees to AWAITING_AVAILABILITY
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

                # Start scheduling with the first leftover
                if unscheduled:
                    self.process_scheduling_for_interviewee(conversation_id, unscheduled[0]['number'])
                else:
                    # If no leftover interviewees, complete the conversation
                    self.complete_conversation(conversation_id)
            else:
                # No valid slots found in the message
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
                # Could not parse
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
        """
        Handle interviewee messages focusing on slot acceptance or denial.
        """
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
        """
        If interviewee accepts a slot, remove it from availability.
        """
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

        # Update the conversation
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': available_slots
        })

        # Finalize (create calendar event, send confirmations, etc.)
        self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def _handle_slot_denial(self, conversation_id: str, interviewee: dict, conversation: dict):
        """
        Interviewee denies the proposed slot -> mark it 'offered' for them, free it from reserved, 
        but do NOT remove from global availability so others can still use it.
        """
        reserved_slots = conversation.get('reserved_slots', [])
        available_slots = conversation.get('available_slots', [])

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

        # Check if they have more untried slots left
        untried_slots = self._get_untried_slots_for_interviewee(interviewee, available_slots, reserved_slots)
        if untried_slots:
            interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
        else:
            interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value

        # Update conversation
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee['number']:
                conversation['interviewees'][i] = interviewee

        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees'],
            'reserved_slots': reserved_slots,
            'available_slots': available_slots
        })

        # Either offer them the next slot or proceed to check others
        if interviewee['state'] == ConversationState.AWAITING_AVAILABILITY.value:
            self.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
        else:
            self.process_remaining_interviewees(conversation_id)

    def process_remaining_interviewees(self, conversation_id: str):
        """
        This method is called whenever an interviewee denies or finishes scheduling.
        It ensures all interviewees who might have newly freed slots get a chance to see them 
        before we ask the interviewer for more availability.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            return

        # We'll loop until no changes occur (meaning we can't revert NO_SLOTS_AVAILABLE to AWAITING_AVAILABILITY anymore)
        while True:
            # 1) If anyone is in CONFIRMATION_PENDING, we stop. We must wait for them to respond.
            pending = [ie for ie in conversation['interviewees']
                       if ie['state'] == ConversationState.CONFIRMATION_PENDING.value]
            if pending:
                return  # Wait for the pending interviewee(s) to respond

            # 2) If there's someone in AWAITING_AVAILABILITY, schedule them.
            awaiting = [ie for ie in conversation['interviewees']
                        if ie['state'] == ConversationState.AWAITING_AVAILABILITY.value]
            if awaiting:
                # Process the first one; we do one at a time
                self.process_scheduling_for_interviewee(conversation_id, awaiting[0]['number'])
                # Re-fetch conversation in case changes happen
                conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
                continue  # Loop again to see if there's more

            # 3) For each NO_SLOTS_AVAILABLE, see if a newly-freed slot is actually now available
            # If so, revert them to AWAITING_AVAILABILITY
            no_slots = [ie for ie in conversation['interviewees']
                        if ie['state'] == ConversationState.NO_SLOTS_AVAILABLE.value]
            if not no_slots:
                # If none are NO_SLOTS_AVAILABLE, we break out to final checks
                break

            available_slots = conversation.get('available_slots', [])
            reserved_slots = conversation.get('reserved_slots', [])
            updated_any = False

            for ie in no_slots:
                untried = self._get_untried_slots_for_interviewee(ie, available_slots, reserved_slots)
                if untried:
                    ie['state'] = ConversationState.AWAITING_AVAILABILITY.value
                    updated_any = True

            if updated_any:
                # Update the conversation for these changes
                self.scheduler.mongodb_handler.update_conversation(
                    conversation['conversation_id'],
                    {'interviewees': conversation['interviewees']}
                )
                # Then continue the loop, so we can process newly AWAITING_AVAILABILITY interviewees
                conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
                continue
            else:
                # If we didn't update anyone, break out
                break

        # If we are here, there's no one in AWAITING_AVAILABILITY or CONFIRMATION_PENDING
        # Some might remain NO_SLOTS_AVAILABLE or be CANCELLED or SCHEDULED
        unscheduled = [
            ie for ie in conversation['interviewees']
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]
        ]

        if unscheduled:
            # We have interviewees who still can't be scheduled
            if conversation.get('final_scheduling_round'):
                # If we've already asked interviewer for more slots once, let's finalize
                self.complete_conversation(conversation_id)
            else:
                # Ask interviewer for more slots
                self._request_more_slots(conversation_id, unscheduled, conversation)
        else:
            # Everyone is scheduled or cancelled
            self.complete_conversation(conversation_id)

    def process_scheduling_for_interviewee(self, conversation_id: str, interviewee_number: str):
        """
        Offer the next untried slot to a specific interviewee.
        """
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found.")
            return

        interviewee = next((ie for ie in conversation['interviewees']
                            if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        # If already pending, wait
        if interviewee['state'] == ConversationState.CONFIRMATION_PENDING.value:
            return

        available_slots = conversation.get('available_slots', [])
        reserved_slots = conversation.get('reserved_slots', [])

        untried = self._get_untried_slots_for_interviewee(interviewee, available_slots, reserved_slots)
        if untried:
            # Offer the first untried slot
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

            # Send that slot to the interviewee
            timezone_str = interviewee.get('timezone', 'UTC')
            localized_start_time = datetime.fromisoformat(next_slot['start_time']).astimezone(
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

            # Move on to see if we can help others or if we must request more slots
            self.process_remaining_interviewees(conversation_id)

    def _get_untried_slots_for_interviewee(self, interviewee: dict, available_slots: list, reserved_slots: list) -> list:
        """
        Return all globally available slots that haven't been offered to this interviewee yet
        and are not currently reserved by them.
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
        Ask the interviewer for more availability after we've used all currently known slots.
        """
        interviewer = conversation.get('interviewer')
        if not interviewer:
            return

        interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'interviewer': interviewer
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
        Mark the conversation as completed and notify the interviewer. 
        """
        try:
            conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found.")
                return

            unscheduled = [
                ie['name'] for ie in conversation['interviewees']
                if ie['state'] in [ConversationState.NO_SLOTS_AVAILABLE.value,
                                   ConversationState.AWAITING_AVAILABILITY.value]
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

        except Exception as e:
            logger.error(f"Error completing conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())

    def send_reminder(self, conversation_id: str, participant_id: str):
        """
        Send a reminder to the participant if they haven't replied in a while.
        """
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
        """
        Update participant's timezone and prompt them for availability.
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

            # Notify them
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
        """
        If an interviewee doesn't have a timezone set, prompt them. Otherwise, process scheduling.
        """
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
            # Ask for timezone
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
        """
        Re-attempt scheduling for those previously NO_SLOTS_AVAILABLE after new slots are added.
        """
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
        """
        Start scheduling for interviewees who are AWAITING_AVAILABILITY.
        """
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
        """
        Handle general queries (both interviewer and interviewee).
        """
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
        """
        Handle an interview cancellation request from the interviewer.
        """
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

            # Possibly complete if no scheduling remains
            if self.scheduler.is_conversation_complete(conversation):
                self.complete_conversation(conversation_id)

        else:
            # Prompt interviewer for which interviewee to cancel
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
        """
        Handle an interview cancellation request from an interviewee.
        """
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
            # Prompt for the name
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
        """
        The interviewer wants to reschedule a meeting that's already on the calendar.
        """
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
            # Multiple scheduled interviews found, ask for which to reschedule
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
        """
        The interviewee wants to reschedule a meeting that's already on the calendar.
        """
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
        """
        Create a unique, hashable key from a slot dictionary based on 'start_time'.
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
