# chatbot/conversation.py
from datetime import datetime, timedelta
import pytz
import logging
import json
import os
import traceback
import uuid
from typing import Optional, Dict, Any

from .attention import AttentionFlagManager
from .schedule_api import ScheduleAPI
from .message_handler import MessageHandler
from chatbot.utils import normalize_number
from chatbot.constants import ConversationState, AttentionFlag
from dotenv import load_dotenv
from store.mongodb_handler import MongoDBHandler
from calendar_module.calendar_service import CalendarService
from chatbot.schedule_api import ScheduleAPI

load_dotenv()

your_mongodb_uri = os.getenv("MONGODB_URI")
your_db_name = os.getenv("MONGODB_DB_NAME")

logger = logging.getLogger(__name__)

class AttentionFlagEvaluator:
    RESPONSE_THRESHOLD = timedelta(hours=24)

    def evaluate_conversation_flags(self, conversation: Dict[str, Any], current_time: datetime):
        flags = {}
        interviewer = conversation['interviewer']
        interviewer_flags = self.evaluate_participant_flags(conversation, 'interviewer', interviewer, current_time)
        if interviewer_flags:
            flags['interviewer'] = interviewer_flags

        for interviewee in conversation['interviewees']:
            participant_id = interviewee['number']
            participant_flags = self.evaluate_participant_flags(conversation, participant_id, interviewee, current_time)
            if participant_flags:
                flags[participant_id] = participant_flags

        return flags

    def evaluate_participant_flags(self, conversation: Dict[str, Any], participant_id: str, participant: Dict[str, Any], current_time: datetime):
        participant_flags = set()
        last_response_times = conversation.get('last_response_times', {})
        last_response = last_response_times.get(participant_id)

        if last_response and (current_time - last_response) > self.RESPONSE_THRESHOLD:
            participant_flags.add(AttentionFlag.NO_RESPONSE)

        if participant.get('scheduled_slot'):
            meeting_time = datetime.fromisoformat(participant['scheduled_slot']['start_time'])
            if current_time > meeting_time and (current_time - meeting_time) < timedelta(hours=1):
                participant_flags.add(AttentionFlag.MISSED_SCHEDULED_MEETING)

        if participant.get('state') == ConversationState.NO_SLOTS_AVAILABLE.value:
            participant_flags.add(AttentionFlag.NO_AVAILABLE_SLOTS)

        return participant_flags


class AttentionFlagHandler:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def handle_flags_for_conversation(self, conversation_id, flags_dict):
        all_flags = set()
        for fset in flags_dict.values():
            all_flags.update(fset)

        if all_flags:
            self.notify_contact_person(conversation_id, all_flags)

    def notify_contact_person(self, conversation_id: str, flags):
        conversation = self.scheduler.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for notifying contact person.")
            return

        contact_name = conversation['role_to_contact_name']
        contact_number = conversation['role_to_contact_number']

        notification_lines = []
        for flag in flags:
            if flag == AttentionFlag.NO_RESPONSE:
                notification_lines.append("We haven't heard from a participant in over 24 hours.")
            elif flag == AttentionFlag.MISSED_SCHEDULED_MEETING:
                notification_lines.append("A participant appears to have missed their scheduled meeting.")
            elif flag == AttentionFlag.NO_AVAILABLE_SLOTS:
                notification_lines.append("No more available slots are available for an interviewee.")

        notification_message = f"Attention {contact_name},\n" + "\n".join(notification_lines) + "\nPlease assist."
        self.scheduler.message_handler.send_message(contact_number, notification_message)
        self.scheduler.log_conversation(conversation_id, 'system', 'notification', notification_message, 'System')


class InterviewScheduler:
    def __init__(self):
        self.attention_manager = AttentionFlagManager()
        self.api_handler = ScheduleAPI()
        self.message_handler = MessageHandler(self)
        self.mongodb_handler = MongoDBHandler(your_mongodb_uri, your_db_name)
        self.calendar_service = CalendarService()
        self.schedule_api = ScheduleAPI()

        self.evaluator = AttentionFlagEvaluator()
        self.flag_handler = AttentionFlagHandler(self)

        self.setup_conversation_logger()

    def check_attention_flags(self):
        current_time = datetime.now(pytz.UTC)
        conversations = self.mongodb_handler.get_all_conversations()

        for conversation in conversations:
            conversation_id = conversation['conversation_id']
            flags_dict = self.evaluator.evaluate_conversation_flags(conversation, current_time)
            if flags_dict:
                self.flag_handler.handle_flags_for_conversation(conversation_id, flags_dict)

    def notify_contact_person(self, conversation_id: str, flagged_participant_id: Optional[str], flag_type: AttentionFlag):
        self.flag_handler.notify_contact_person(conversation_id, {flag_type})

    def setup_conversation_logger(self):
        self.conversation_logger = logging.getLogger('conversation_history')
        conversation_handler = logging.FileHandler('conversation_history.log')
        conversation_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(message)s')
        )
        self.conversation_logger.addHandler(conversation_handler)
        self.conversation_logger.setLevel(logging.INFO)

    def log_conversation_history(self, conversation_id: str):
        try:
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found for logging history.")
                return

            history = {
                'conversation_id': conversation_id,
                'timestamp': datetime.now().isoformat(),
                'interviewer': {
                    'name': conversation['interviewer']['name'],
                    'history': conversation['interviewer']['conversation_history']
                },
                'interviewees': [
                    {
                        'name': interviewee['name'],
                        'history': interviewee['conversation_history']
                    }
                    for interviewee in conversation['interviewees']
                ],
                'scheduled_slots': conversation.get('scheduled_slots', []),
                'state': {
                    'interviewer': conversation['interviewer']['state'],
                    'interviewees': [
                        {
                            'number': ie['number'],
                            'state': ie['state']
                        }
                        for ie in conversation['interviewees']
                    ]
                }
            }
            self.conversation_logger.info(json.dumps(history, indent=2))
        except Exception as e:
            logger.error(f"Error logging conversation history: {str(e)}")
            logger.error(traceback.format_exc())

    def _create_participant_dict(
        self,
        name: str,
        number: str,
        email: str,
        role: str,
        superior_flag: str,
        meeting_duration: int,
        role_to_contact_name: str,
        role_to_contact_number: str,
        role_to_contact_email: str,
        company_details: str,
        jd_title: str = ""
    ) -> dict:
        return {
            'name': name,
            'number': number,
            'email': email,
            'role': role,
            'superior_flag': superior_flag,
            'meeting_duration': meeting_duration,
            'conversation_history': [],
            'slots': None,
            'state': ConversationState.AWAITING_AVAILABILITY.value,
            'timezone': None,
            'confirmed': False,
            'role_to_contact_name': role_to_contact_name,
            'role_to_contact_number': role_to_contact_number,
            'role_to_contact_email': role_to_contact_email,
            'company_details': company_details,
            'confirmation_sent': False,
            'scheduled_slot': None,
            'out_of_context_count': 0,
            'cancellation_count': 0,
            'reschedule_count': 0,
            'jd_title': jd_title
        }

    def start_conversation(self, interviewer_name, interviewer_number, interviewer_email, interviewees_data, superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number, role_to_contact_email, company_details) -> str:
        if not all([interviewer_number, interviewer_name]):
            raise ValueError("Interviewer information must be provided")

        if not interviewees_data or not isinstance(interviewees_data, list):
            raise ValueError("Interviewees information must be a non-empty list")

        if not isinstance(meeting_duration, int) or int(meeting_duration) <= 0:
            raise ValueError("Meeting duration must be a positive integer")

        interviewer_number = normalize_number(interviewer_number)

        conversation_id = str(uuid.uuid4())

        interviewer = self._create_participant_dict(
            name=interviewer_name,
            number=interviewer_number,
            email=interviewer_email,
            role='interviewer',
            superior_flag=superior_flag,
            meeting_duration=meeting_duration,
            role_to_contact_name=role_to_contact_name,
            role_to_contact_number=role_to_contact_number,
            role_to_contact_email=role_to_contact_email,
            company_details=company_details
        )

        interviewees = []
        for interviewee_data in interviewees_data:
            interviewee = self._create_participant_dict(
                name=interviewee_data['name'],
                number=normalize_number(interviewee_data['number']),
                email=interviewee_data['email'],
                role='interviewee',
                superior_flag=superior_flag,
                meeting_duration=meeting_duration,
                role_to_contact_name=role_to_contact_name,
                role_to_contact_number=role_to_contact_number,
                role_to_contact_email=role_to_contact_email,
                company_details=company_details,
                jd_title=interviewee_data['jd_title']
            )
            interviewees.append(interviewee)

        conversation_data = {
            'conversation_id': conversation_id,
            'interviewer': interviewer,
            'interviewees': interviewees,
            'alternate_slots_requested': False,
            'created_at': datetime.now().isoformat(),
            'scheduled_slots': [],
            'role_to_contact_name': role_to_contact_name,
            'role_to_contact_number': role_to_contact_number,
            'role_to_contact_email': role_to_contact_email,
            'company_details': company_details,
            'available_slots': [],
            'archived_slots': [],
            'last_response_times': {}
        }

        self.mongodb_handler.create_conversation(conversation_data)
        self.initiate_conversation_with_interviewer(conversation_id)
        logger.info(f"New conversation started: {conversation_id}")
        self.log_conversation_history(conversation_id)
        return conversation_id

    def initiate_conversation_with_interviewer(self, conversation_id):
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiation.")
            return

        interviewer = conversation['interviewer']
        interviewees = conversation.get('interviewees', [])

        if not interviewees:
            candidates_info = "Currently, there are no candidates assigned for interviews."
        else:
            candidates_info_list = [
                f"{interviewee['name']} (JD Title: {interviewee.get('jd_title', 'N/A')})"
                for interviewee in interviewees
            ]
            candidates_info = "Here are the candidates assigned:\n" + "\n".join(candidates_info_list)

        system_message = f"""Hello {interviewer['name']}, I’m here to assist with scheduling interviews for the upcoming candidates. Could you please provide your availability for the coming week?

Feel free to share any specific time preferences or constraints. Once I have your availability, I’ll coordinate with the candidates.

##Details about the interview - 
{candidates_info}
Duration - {interviewer['meeting_duration']}
"""

        response = self.message_handler.generate_response(
            interviewer,
            None,
            "Null",
            system_message,
            conversation_state=interviewer['state']
        )
        self.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.message_handler.send_message(interviewer['number'], response)

    def process_scheduling_for_interviewee(self, conversation_id, interviewee_number):
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for scheduling.")
            return

        interviewer = conversation['interviewer']
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        interviewer_slots = interviewer.get('slots', {})
        if not conversation.get('available_slots'):
            available_slots = interviewer_slots.get('time_slots', [])
            self.mongodb_handler.update_conversation(conversation_id, {'available_slots': available_slots})
            conversation = self.mongodb_handler.get_conversation(conversation_id)

        offered_slots = interviewee.get('offered_slots', [])
        slots_to_offer = [slot for slot in conversation['available_slots'] if slot not in offered_slots]

        if not slots_to_offer and conversation.get('archived_slots'):
            slots_to_offer = [slot for slot in conversation['archived_slots'] if slot not in offered_slots]

        if not slots_to_offer:
            asked_for_more_slots = conversation.get('asked_for_more_slots', False)
            if not asked_for_more_slots:
                self.mongodb_handler.update_conversation(conversation_id, {'asked_for_more_slots': True})
                conversation = self.mongodb_handler.get_conversation(conversation_id)

                # Update interviewee state
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == interviewee_number:
                        ie['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
                        conversation['interviewees'][i] = ie
                self.mongodb_handler.update_conversation(conversation_id, {'interviewees': conversation['interviewees']})
                conversation = self.mongodb_handler.get_conversation(conversation_id)

                system_message = (
                    f"Hello {interviewer['name']}, we have run out of available slots for some interviewees. "
                    f"Could you please provide more availability?"
                )
                response = self.message_handler.generate_response(
                    interviewer,
                    None,
                    "",
                    system_message,
                    conversation_state=interviewer['state']
                )
                self.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.message_handler.send_message(interviewer['number'], response)

                interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
                self.mongodb_handler.update_conversation(conversation_id, {'interviewer': interviewer})
            else:
                # No additional slots have been provided
                unscheduled_interviewees = [ie for ie in conversation['interviewees']
                                            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]]

                for ie in unscheduled_interviewees:
                    sys_msg = (
                        "We couldn't find any more available slots for your interview. "
                        "We will reach out later if new slots become available."
                    )
                    response = self.message_handler.generate_response(
                        ie,
                        interviewer,
                        "",
                        sys_msg,
                        conversation_state=ie['state']
                    )
                    self.log_conversation(conversation_id, ie['number'], "system", response, "AI")
                    self.message_handler.send_message(ie['number'], response)
                    ie['state'] = ConversationState.NO_SLOTS_AVAILABLE.value

                self.mongodb_handler.update_conversation(conversation_id, {'interviewees': conversation['interviewees']})
                conversation = self.mongodb_handler.get_conversation(conversation_id)

                sys_msg = "No additional slots provided. We have informed the remaining interviewees that no slots are available."
                response = self.message_handler.generate_response(
                    interviewer,
                    None,
                    "",
                    sys_msg,
                    conversation_state=interviewer['state']
                )
                self.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.message_handler.send_message(interviewer['number'], response)

                self.notify_contact_person(conversation_id, None, AttentionFlag.NO_AVAILABLE_SLOTS)

            return

        # Offer a slot
        proposed_slot = slots_to_offer[0]
        if 'offered_slots' not in interviewee:
            interviewee['offered_slots'] = []
        interviewee['offered_slots'].append(proposed_slot)
        # Update interviewee in DB
        for i, ie in enumerate(conversation['interviewees']):
            if ie['number'] == interviewee_number:
                ie.update({
                    'offered_slots': interviewee['offered_slots'],
                    'proposed_slot': proposed_slot,
                    'state': ConversationState.CONFIRMATION_PENDING.value
                })
                conversation['interviewees'][i] = ie
        self.mongodb_handler.update_conversation(conversation_id, {'interviewees': conversation['interviewees']})
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)

        interviewee_timezone = interviewee.get('timezone', 'UTC')
        meeting_time_utc = datetime.fromisoformat(proposed_slot['start_time'])
        localized_meeting_time = meeting_time_utc.astimezone(pytz.timezone(interviewee_timezone))

        system_message = f"A proposed meeting time is: {localized_meeting_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}. Please confirm if this works."
        response = self.message_handler.generate_response(
            interviewee,
            interviewer,
            "",
            system_message,
            conversation_state=interviewee['state']
        )
        self.log_conversation(conversation_id, interviewee_number, "system", response, "AI")
        self.message_handler.send_message(interviewee['number'], response)

    def finalize_scheduling_for_interviewee(self, conversation_id, interviewee_number):
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for finalizing scheduling.")
            return

        interviewer = conversation['interviewer']
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)
        if not interviewee or not interviewee.get('proposed_slot'):
            logger.error(f"No proposed slot found for interviewee {interviewee_number} in conversation {conversation_id}.")
            return

        try:
            meeting_time_utc = datetime.fromisoformat(interviewee['proposed_slot']['start_time'])

            # Update interviewee's scheduled slot and state
            interviewee['scheduled_slot'] = interviewee['proposed_slot']
            interviewee['state'] = ConversationState.SCHEDULED.value

            # Remove the scheduled slot from available slots
            available_slots = conversation['available_slots']
            if interviewee['proposed_slot'] in available_slots:
                available_slots.remove(interviewee['proposed_slot'])

            # Clear proposed_slot
            interviewee['confirmed'] = False
            interviewee['proposed_slot'] = None

            # Update conversation with modified interviewee and slots
            for i, ie in enumerate(conversation['interviewees']):
                if ie['number'] == interviewee_number:
                    conversation['interviewees'][i] = interviewee

            if 'scheduled_slots' not in conversation:
                conversation['scheduled_slots'] = []
            conversation['scheduled_slots'].append(interviewee['scheduled_slot'])

            update_data = {
                'interviewees': conversation['interviewees'],
                'available_slots': available_slots,
                'scheduled_slots': conversation['scheduled_slots']
            }
            self.mongodb_handler.update_conversation(conversation_id, update_data)
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            interviewer = conversation['interviewer']
            interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)

            # Notify both participants
            for participant in [interviewer, interviewee]:
                participant_timezone = participant.get('timezone', 'UTC')
                try:
                    tz = pytz.timezone(participant_timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    tz = pytz.UTC
                localized_meeting_time = meeting_time_utc.astimezone(tz)
                system_message = f"Your meeting is scheduled for {localized_meeting_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}."
                response = self.message_handler.generate_response(
                    participant,
                    interviewer if participant['role'] == 'interviewee' else interviewee,
                    "",
                    system_message,
                    conversation_state=participant['state']
                )
                self.log_conversation(conversation_id, participant['number'], "system", response, "AI")
                self.message_handler.send_message(participant['number'], response)

            # Create the calendar event
            event_result = self.api_handler.post_to_create_event(conversation_id, interviewee_number)
            if event_result:
                event_id = event_result.get('event_id')
                if event_id:
                    # Update the interviewee with the event_id
                    for i, ie in enumerate(conversation['interviewees']):
                        if ie['number'] == interviewee_number:
                            ie['event_id'] = event_id
                            conversation['interviewees'][i] = ie
                    self.mongodb_handler.update_conversation(conversation_id, {'interviewees': conversation['interviewees']})
                else:
                    logger.error(f"Failed to retrieve event_id for conversation {conversation_id} and interviewee {interviewee_number}.")
            else:
                logger.error(f"Failed to create event for conversation {conversation_id} and interviewee {interviewee_number}.")

            self.initiate_next_interviewee(conversation_id)

        except Exception as e:
            logger.error(f"Error finalizing scheduling for interviewee {interviewee_number} in conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())

    def initiate_next_interviewee(self, conversation_id):
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiating next interviewee.")
            return

        for interviewee in conversation['interviewees']:
            if interviewee['state'] == ConversationState.AWAITING_AVAILABILITY.value:
                self.message_handler.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])
                return

        logger.info(f"All interviewees have been contacted or scheduled for conversation {conversation_id}.")

    def log_conversation(self, conversation_id: str, participant_id: str, message_type: str, message: str, sender: str) -> None:
        try:
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found for logging.")
                return

            participant = None
            if participant_id == 'interviewer':
                participant = conversation['interviewer']
            else:
                participant = next((ie for ie in conversation['interviewees'] if ie['number'] == participant_id), None)

            if not participant:
                logger.error(f"Participant {participant_id} not found in conversation {conversation_id} for logging.")
                return

            log_entry = f"{sender}: {message_type.capitalize()}: {message}"
            participant_history = participant.get('conversation_history', [])
            participant_history.append(log_entry)

            if participant['role'] == 'interviewer':
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer.conversation_history': participant_history
                })
            else:
                # Update the specific interviewee
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == participant_id:
                        conversation['interviewees'][i]['conversation_history'] = participant_history
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

            logger.debug(f"Logged message for participant {participant_id} in conversation {conversation_id}: {log_entry}")
        except Exception as e:
            logger.error(f"Error logging conversation: {str(e)}")

    def change_meeting_duration(self, conversation_id: str, new_duration: int):
        if new_duration <= 0:
            raise ValueError("Meeting duration must be positive.")

        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for changing meeting duration.")
            return False

        # Update meeting_duration for interviewer
        conversation['interviewer']['meeting_duration'] = new_duration

        # Update meeting_duration for each interviewee
        for ie in conversation['interviewees']:
            ie['meeting_duration'] = new_duration

        # Resize available_slots
        for slot in conversation['available_slots']:
            start_dt = datetime.fromisoformat(slot['start_time'])
            end_dt = start_dt + timedelta(minutes=new_duration)
            slot['end_time'] = end_dt.isoformat()

        # Resize archived_slots
        for slot in conversation['archived_slots']:
            start_dt = datetime.fromisoformat(slot['start_time'])
            end_dt = start_dt + timedelta(minutes=new_duration)
            slot['end_time'] = end_dt.isoformat()

        # Resize scheduled_slots
        for scheduled_slot in conversation.get('scheduled_slots', []):
            start_dt = datetime.fromisoformat(scheduled_slot['start_time'])
            end_dt = start_dt + timedelta(minutes=new_duration)
            scheduled_slot['end_time'] = end_dt.isoformat()

        self.mongodb_handler.update_conversation(conversation_id, {
            'interviewer': conversation['interviewer'],
            'interviewees': conversation['interviewees'],
            'available_slots': conversation['available_slots'],
            'archived_slots': conversation['archived_slots'],
            'scheduled_slots': conversation.get('scheduled_slots', [])
        })

        # Update each scheduled event in Google Calendar
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        for ie in conversation['interviewees']:
            if ie.get('state') == ConversationState.SCHEDULED.value and ie.get('scheduled_slot') and ie.get('event_id'):
                event_id = ie['event_id']
                start_dt = datetime.fromisoformat(ie['scheduled_slot']['start_time'])
                end_dt = start_dt + timedelta(minutes=new_duration)
                ie['scheduled_slot']['end_time'] = end_dt.isoformat()
                update_success = self.calendar_service.update_event(conversation_id, event_id, start_dt.isoformat(), end_dt.isoformat())
                if not update_success:
                    logger.error(f"Failed to update event {event_id} in conversation {conversation_id}.")

        logger.info(f"Meeting duration changed to {new_duration} for conversation {conversation_id}.")
        return True

scheduler = InterviewScheduler()
