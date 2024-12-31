from datetime import datetime, timedelta
import pytz
import logging
import json
import os
import traceback
import uuid
from typing import Optional, Dict, Any, List

from .attention import AttentionFlagManager
from .schedule_api import ScheduleAPI
from .message_handler import MessageHandler
from chatbot.utils import normalize_number, get_localized_current_time, extract_timezone_from_number
from chatbot.constants import ConversationState, AttentionFlag
from dotenv import load_dotenv
from store.mongodb_handler import MongoDBHandler
from calendar_module.calendar_service import CalendarService
from chatbot.schedule_api import ScheduleAPI
import threading

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
            # Store flags in the database
            self.store_attention_flags(conversation_id, all_flags)

    def store_attention_flags(self, conversation_id: str, flags: set):
        for flag in flags:
            flag_id = str(uuid.uuid4())
            flag_entry = {
                'id': flag_id,
                'conversation_id': conversation_id,
                'flag_type': flag.value,
                'message': self.generate_flag_message(flag),
                'severity': 'high',
                'created_at': datetime.now(pytz.UTC).isoformat(),
                'resolved': False
            }
            self.scheduler.mongodb_handler.create_attention_flag(flag_entry)
            logger.info(f"Stored attention flag {flag_id} for conversation {conversation_id}.")

    def generate_flag_message(self, flag: AttentionFlag) -> str:
        if flag == AttentionFlag.NO_RESPONSE:
            return "No response from participant for over 24 hours."
        elif flag == AttentionFlag.MISSED_SCHEDULED_MEETING:
            return "Participant missed their scheduled meeting."
        elif flag == AttentionFlag.NO_AVAILABLE_SLOTS:
            return "No available slots left for interviewees."
        else:
            return "Unknown attention flag."


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

        # Initialize conversation queues with thread-safe mechanisms
        self.conversation_queues: Dict[str, List[str]] = {}
        self.queue_lock = threading.Lock()

    def check_attention_flags(self):
        current_time = datetime.now(pytz.UTC)
        conversations = self.mongodb_handler.get_all_conversations()

        for conversation in conversations:
            conversation_id = conversation['conversation_id']
            flags_dict = self.evaluator.evaluate_conversation_flags(conversation, current_time)
            if flags_dict:
                self.flag_handler.handle_flags_for_conversation(conversation_id, flags_dict)

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

    def start_conversation(self, interviewer_name, interviewer_number, interviewer_email, interviewees_data,
                           superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number,
                           role_to_contact_email, company_details) -> str:

        if not all([interviewer_number, interviewer_name]):
            raise ValueError("Interviewer information must be provided")

        if not interviewees_data or not isinstance(interviewees_data, list):
            raise ValueError("Interviewees information must be a non-empty list")

        if not isinstance(meeting_duration, int) or int(meeting_duration) <= 0:
            raise ValueError("Meeting duration must be a positive integer")

        interviewer_number = normalize_number(interviewer_number)

        # Check if the interviewer has active conversations
        active_conversations = self.mongodb_handler.find_active_conversations_by_interviewer(interviewer_number)
        if active_conversations:
            # Add to queue
            conversation_id = str(uuid.uuid4())
            conversation_data = {
                'conversation_id': conversation_id,
                'interviewer': self._create_participant_dict(
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
                ),
                'interviewees': [
                    self._create_participant_dict(
                        name=ie['name'],
                        number=normalize_number(ie['number']),
                        email=ie['email'],
                        role='interviewee',
                        superior_flag=superior_flag,
                        meeting_duration=meeting_duration,
                        role_to_contact_name=role_to_contact_name,
                        role_to_contact_number=role_to_contact_number,
                        role_to_contact_email=role_to_contact_email,
                        company_details=company_details,
                        jd_title=ie.get('jd_title', "")
                    )
                    for ie in interviewees_data
                ],
                'alternate_slots_requested': False,
                'created_at': datetime.now().isoformat(),
                'scheduled_slots': [],
                'role_to_contact_name': role_to_contact_name,
                'role_to_contact_number': role_to_contact_number,
                'role_to_contact_email': role_to_contact_email,
                'company_details': company_details,
                'available_slots': [],
                'archived_slots': [],
                'last_response_times': {},
                'status': 'queued',
                'more_slots_requests': 0,
                'last_more_slots_request_time': None,
                'slot_denials': {}
            }
            self.mongodb_handler.create_conversation(conversation_data)
            self.enqueue_conversation(interviewer_number, conversation_id)
            logger.info(f"New conversation queued for interviewer {interviewer_number}: {conversation_id}")
            self.log_conversation_history(conversation_id)
            return conversation_id

        # No active conversations, proceed normally
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
                jd_title=interviewee_data.get('jd_title', "")
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
            'last_response_times': {},
            'status': 'active',
            'more_slots_requests': 0,
            'last_more_slots_request_time': None,
            'slot_denials': {}
        }

        self.mongodb_handler.create_conversation(conversation_data)
        self.initiate_conversation_with_interviewer(conversation_id)
        logger.info(f"New conversation started: {conversation_id}")
        self.log_conversation_history(conversation_id)
        return conversation_id

    def enqueue_conversation(self, interviewer_number: str, conversation_id: str):
        with self.queue_lock:
            if interviewer_number not in self.conversation_queues:
                self.conversation_queues[interviewer_number] = []
            self.conversation_queues[interviewer_number].append(conversation_id)
            logger.debug(f"Conversation {conversation_id} enqueued for interviewer {interviewer_number}.")

    def dequeue_conversation(self, interviewer_number: str) -> Optional[str]:
        with self.queue_lock:
            if interviewer_number in self.conversation_queues and self.conversation_queues[interviewer_number]:
                conversation_id = self.conversation_queues[interviewer_number].pop(0)
                logger.debug(f"Conversation {conversation_id} dequeued for interviewer {interviewer_number}.")
                return conversation_id
            return None

    def remove_conversation_from_queue(self, conversation_id: str):
        with self.queue_lock:
            for interviewer, queue in list(self.conversation_queues.items()):
                if conversation_id in queue:
                    queue.remove(conversation_id)
                    logger.info(f"Conversation {conversation_id} removed from queue of interviewer {interviewer}.")
                    if not queue:
                        del self.conversation_queues[interviewer]
                    break

    def initiate_conversation_with_interviewer(self, conversation_id):
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiation.")
            return
        self.handle_timezone_determination(conversation_id)
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

        timezone_str = interviewer.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        system_message = f"""Say Hello to {interviewer['name']}, and tell him that you are here to assist with scheduling interviews for the upcoming candidates. Also ask them to provide their availability.

Tell them to feel free to share any specific time preferences or constraints and once you have their availability, you’ll coordinate with the candidates.

Current Time: {current_time}

##Details about the interview - 
Candidates_Info -

{candidates_info}
Duration - {interviewer['meeting_duration']} minutes
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

    def finalize_scheduling_for_interviewee(self, conversation_id, interviewee_number):
        """Once an interviewee confirms, do not spam the interviewer. Only notify the interviewee."""
        try:
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

                # Store the scheduled slot
                interviewee['scheduled_slot'] = interviewee['proposed_slot']
                interviewee['state'] = ConversationState.SCHEDULED.value

                # Remove the scheduled slot from available_slots
                if interviewee['proposed_slot'] in conversation['available_slots']:
                    conversation['available_slots'].remove(interviewee['proposed_slot'])
                    self.mongodb_handler.update_conversation(conversation_id, {
                        'available_slots': conversation['available_slots']
                    })

                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                ### ADDED: Only send confirmation to the interviewee, not the interviewer
                participant = interviewee
                try:
                    participant_timezone = participant.get('timezone', 'UTC')
                    try:
                        tz = pytz.timezone(participant_timezone)
                    except pytz.exceptions.UnknownTimeZoneError:
                        logger.warning(f"Invalid timezone {participant_timezone}, defaulting to UTC")
                        tz = pytz.UTC

                    localized_meeting_time = meeting_time_utc.astimezone(tz)
                    system_message = (
                        f"Your meeting has been scheduled for "
                        f"{localized_meeting_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}."
                    )

                    response = self.message_handler.generate_response(
                        participant,
                        interviewer,  # so the LLM knows who else is in conversation
                        "",
                        system_message,
                        conversation_state=participant['state']
                    )
                    self.log_conversation(conversation_id, participant['number'], "system", response, "AI")
                    self.message_handler.send_message(participant['number'], response)

                except Exception as e:
                    logger.error(f"Error sending confirmation to participant {participant['number']}: {str(e)}")

                interviewee['confirmed'] = False
                interviewee['proposed_slot'] = None
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                # Add the scheduled slot to scheduled_slots
                if 'scheduled_slots' not in conversation:
                    conversation['scheduled_slots'] = []
                conversation['scheduled_slots'].append(interviewee['scheduled_slot'])
                self.mongodb_handler.update_conversation(conversation_id, {
                    'scheduled_slots': conversation['scheduled_slots']
                })

                # Create Google Calendar Event
                event_result = self.api_handler.post_to_create_event(conversation_id, interviewee_number)
                logger.info(f"event_result: {event_result}")
                if event_result:
                    interviewee['event_id'] = event_result.get('event_id')
                    if not interviewee['event_id']:
                        logger.error(f"Failed to retrieve event_id for conversation {conversation_id} and interviewee {interviewee_number}.")
                    else:
                        logger.info(f"event_id: {interviewee['event_id']}")
                    self.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })
                    logger.info(f"Event created for conversation {conversation_id} and interviewee {interviewee_number}.")
                else:
                    logger.error(f"Failed to create event for conversation {conversation_id} and interviewee {interviewee_number}.")

                # Initiate next interviewee
                self.initiate_next_interviewee(conversation_id)

            except Exception as e:
                logger.error(f"Error finalizing scheduling for interviewee {interviewee_number} in conversation {conversation_id}: {str(e)}")
                logger.error(traceback.format_exc())

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
        self.complete_conversation(conversation_id)

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
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == participant_id:
                        conversation['interviewees'][i]['conversation_history'] = participant_history
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

            logger.debug(f"Logged message for participant {participant_id} in conversation {conversation_id}: {log_entry}")
        except Exception as e:
            logger.error(f"Error logging conversation: {str(e)}")

    def complete_conversation(self, conversation_id: str):
        """
        Just before marking conversation completed, send the interviewer a
        conclusive report about who got scheduled and who did not.
        """
        conversation = self.mongodb_handler.get_conversation(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for completion.")
            return

        interviewer = conversation['interviewer']
        interviewer_number = interviewer['number']
        timezone_str = interviewer.get('timezone', 'UTC')
        current_time = get_localized_current_time(timezone_str)

        ### ADDED: Build conclusive report
        report_lines = []
        for ie in conversation['interviewees']:
            name = ie['name']
            state = ie['state']
            if state == ConversationState.SCHEDULED.value and ie.get('scheduled_slot'):
                # Convert to interviewer's local time
                start_utc = datetime.fromisoformat(ie['scheduled_slot']['start_time'])
                try:
                    tz = pytz.timezone(timezone_str)
                except pytz.exceptions.UnknownTimeZoneError:
                    tz = pytz.UTC
                local_time = start_utc.astimezone(tz)
                local_time_str = local_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')
                report_lines.append(f"{name} => Scheduled at {local_time_str}")
            else:
                # Not scheduled or canceled
                report_lines.append(f"{name} => {state.upper()}")

        final_report = "Here is the final interview schedule:\n\n" + "\n".join(report_lines)
        self.message_handler.send_message(interviewer_number, final_report)
        self.log_conversation(conversation_id, 'interviewer', "system", final_report, "AI")

        # Now mark conversation as completed
        self.mongodb_handler.update_conversation(
            conversation_id,
            {'status': 'completed', 'completed_at': datetime.now().isoformat()}
        )

        system_message = f"The conversation has been marked as completed.\n\nCurrent Time: {current_time}"
        response = self.message_handler.generate_response(
            interviewer,
            None,
            "",
            system_message,
            conversation_state=interviewer['state']
        )
        self.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.message_handler.send_message(interviewer['number'], response)

        self.initiate_next_conversation_if_available(interviewer_number)
        logger.info(f"Conversation {conversation_id} marked as completed.")

    def is_conversation_complete(self, conversation: Dict[str, Any]) -> bool:
        for ie in conversation['interviewees']:
            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]:
                return False
        return True

    def initiate_next_conversation_if_available(self, interviewer_number: str):
        conversation_id = self.dequeue_conversation(interviewer_number)
        if conversation_id:
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            if conversation:
                self.initiate_conversation_with_interviewer(conversation_id)
            else:
                logger.warning(f"Conversation {conversation_id} was dequeued but does not exist.")
                self.initiate_next_conversation_if_available(interviewer_number)

    def determine_timezone_for_participant(self, conversation_id: str, participant: dict) -> str:
        try:
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            timezone = extract_timezone_from_number(participant['number'])
            if timezone and timezone.lower() != 'unspecified':
                return timezone

            # Ask for city
            current_time = get_localized_current_time('UTC')
            name = participant['name']
            role = participant['role']
            system_message = (
                f"Hello {name}! Before we proceed with scheduling, could you please let me know which city you're in? "
                f"This helps me provide accurate scheduling options.\n\n"
                f"Current Time: {current_time}"
            )
            response = self.message_handler.generate_response(
                participant,
                None,
                "",
                system_message,
                conversation_state=ConversationState.TIMEZONE_CLARIFICATION.value
            )
            self.log_conversation(conversation_id, participant['number'], "system", response, "AI")
            self.message_handler.send_message(participant['number'], response)

            if role == 'interviewer':
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer.state': ConversationState.TIMEZONE_CLARIFICATION.value
                })
            else:
                for i, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == participant['number']:
                        ie['state'] = ConversationState.TIMEZONE_CLARIFICATION.value
                        conversation['interviewees'][i] = ie
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

            return None
        except Exception as e:
            logger.error(f"Error determining timezone for participant {participant['number']}: {str(e)}")
            logger.error(traceback.format_exc())
            return 'UTC'

    def handle_timezone_determination(self, conversation_id: str) -> None:
        try:
            conversation = self.mongodb_handler.get_conversation(conversation_id)
            if not conversation:
                logger.error(f"Conversation {conversation_id} not found.")
                return

            interviewer = conversation['interviewer']
            if not interviewer.get('timezone'):
                timezone = self.determine_timezone_for_participant(conversation_id, interviewer)
                if timezone:
                    interviewer['timezone'] = timezone
                    self.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

            for interviewee in conversation['interviewees']:
                if not interviewee.get('timezone'):
                    timezone = self.determine_timezone_for_participant(conversation_id, interviewee)
                    if timezone:
                        for i, ie in enumerate(conversation['interviewees']):
                            if ie['number'] == interviewee['number']:
                                ie['timezone'] = timezone
                        self.mongodb_handler.update_conversation(conversation_id, {
                            'interviewees': conversation['interviewees']
                        })

        except Exception as e:
            logger.error(f"Error handling timezone determination for conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())


scheduler = InterviewScheduler()
