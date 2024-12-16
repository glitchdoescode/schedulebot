# chatbot/conversation.py

from datetime import datetime, timedelta
import pytz
import logging
import json
import os
from .attention import AttentionFlagManager
from .schedule_api import ScheduleAPI
from .message_handler import MessageHandler
from chatbot.utils import normalize_number
from chatbot.constants import ConversationState, AttentionFlag
from dotenv import load_dotenv
import traceback
from store.mongodb_handler import MongoDBHandler
from calendar_module.calendar_service import CalendarService
from chatbot.schedule_api import ScheduleAPI
import uuid  # Added for UUID generation

load_dotenv()

your_mongodb_uri = os.getenv("MONGODB_URI")
your_db_name = os.getenv("MONGODB_DB_NAME")

logger = logging.getLogger(__name__)

class AttentionFlagEvaluator:
    """
    Evaluates which attention flags need to be raised for a conversation.
    """
    RESPONSE_THRESHOLD = timedelta(hours=24)  # Example threshold for no response

    def evaluate_conversation_flags(self, conversation, current_time):
        flags = {}
        # Evaluate interviewer
        interviewer = conversation['interviewer']
        interviewer_flags = self.evaluate_participant_flags(conversation, 'interviewer', interviewer, current_time)
        if interviewer_flags:
            flags['interviewer'] = interviewer_flags

        # Evaluate interviewees
        for interviewee in conversation['interviewees']:
            participant_id = interviewee['number']
            participant_flags = self.evaluate_participant_flags(conversation, participant_id, interviewee, current_time)
            if participant_flags:
                flags[participant_id] = participant_flags

        return flags

    def evaluate_participant_flags(self, conversation, participant_id, participant, current_time):
        participant_flags = set()
        last_response_times = conversation.get('last_response_times', {})
        last_response = last_response_times.get(participant_id)

        # Check no response
        if last_response and (current_time - last_response) > self.RESPONSE_THRESHOLD:
            participant_flags.add(AttentionFlag.NO_RESPONSE)

        # Check missed scheduled meeting
        if participant.get('scheduled_slot'):
            meeting_time = datetime.fromisoformat(participant['scheduled_slot']['start_time'])
            if current_time > meeting_time and (current_time - meeting_time) < timedelta(hours=1):
                participant_flags.add(AttentionFlag.MISSED_SCHEDULED_MEETING)

        # Check no available slots
        if participant.get('state') == ConversationState.NO_SLOTS_AVAILABLE.value:
            participant_flags.add(AttentionFlag.NO_AVAILABLE_SLOTS)

        return participant_flags


class AttentionFlagHandler:
    """
    Handles the attention flags identified by the evaluator.
    """
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def handle_flags_for_conversation(self, conversation_id, flags_dict):
        # Combine all flags
        all_flags = set()
        for fset in flags_dict.values():
            all_flags.update(fset)

        if all_flags:
            self.notify_contact_person(conversation_id, all_flags)

    def notify_contact_person(self, conversation_id, flags):
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
        self.conversations = {}  # In-memory conversations cache
        self.attention_manager = AttentionFlagManager()
        self.api_handler = ScheduleAPI()
        self.message_handler = MessageHandler(self)
        self.mongodb_handler = MongoDBHandler(your_mongodb_uri, your_db_name)  # Initialize MongoDB handler
        self.calendar_service = CalendarService()  # Initialize CalendarService
        self.schedule_api = ScheduleAPI()  # Initialize ScheduleAPI

        self.evaluator = AttentionFlagEvaluator()
        self.flag_handler = AttentionFlagHandler(self)

        # Initialize conversation logger
        self.setup_conversation_logger()

    def check_attention_flags(self):
        """Periodically check for attention flags that need to be raised"""
        current_time = datetime.now(pytz.UTC)
        # Fetch all conversations from MongoDB
        conversations = self.mongodb_handler.get_all_conversations()

        for conversation in conversations:
            conversation_id = conversation['conversation_id']
            # Update in-memory cache
            self.conversations[conversation_id] = conversation

            # Evaluate flags for this conversation
            flags_dict = self.evaluator.evaluate_conversation_flags(conversation, current_time)

            # Handle any triggered flags
            if flags_dict:
                self.flag_handler.handle_flags_for_conversation(conversation_id, flags_dict)

    def notify_contact_person(self, conversation_id: str, flagged_participant_id: str, flag_type: AttentionFlag):
        """This method is still used by some legacy calls, can forward to handle_flags_for_conversation if needed."""
        # For backward compatibility, just handle a single flag
        self.flag_handler.notify_contact_person(conversation_id, {flag_type})

    def setup_conversation_logger(self):
        """Setup a separate logger for conversation history"""
        self.conversation_logger = logging.getLogger('conversation_history')
        conversation_handler = logging.FileHandler('conversation_history.log')
        conversation_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(message)s')
        )
        self.conversation_logger.addHandler(conversation_handler)
        self.conversation_logger.setLevel(logging.INFO)

    def log_conversation_history(self, conversation_id: str):
        """Log the entire conversation history"""
        try:
            conversation = self.conversations.get(conversation_id)
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
                            'number': interviewee['number'],
                            'state': interviewee['state']
                        }
                        for interviewee in conversation['interviewees']
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
        jd_title: str = ""  # New parameter with default empty string
    ) -> dict:
        """
        Create a dictionary containing participant information, including JD title.
        """
        try:
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
                'jd_title': jd_title  # Store JD title
            }
        except Exception as e:
            logger.error(f"Error creating participant dictionary: {str(e)}")
            raise


    def start_conversation(self, interviewer_name, interviewer_number, interviewer_email, interviewees_data, superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number, role_to_contact_email, company_details) -> str:
        """
        Start a new conversation between an interviewer and multiple interviewees.
        Returns the conversation_id.
        """
        try:
            # Input validation
            if not all([interviewer_number, interviewer_name]):
                raise ValueError("Interviewer information must be provided")

            if not interviewees_data or not isinstance(interviewees_data, list):
                raise ValueError("Interviewees information must be a non-empty list")

            if not isinstance(meeting_duration, int) or int(meeting_duration) <= 0:
                raise ValueError("Meeting duration must be a positive integer")

            # Normalize phone numbers
            interviewer_number = normalize_number(interviewer_number)

            # Create conversation ID using UUID
            conversation_id = str(uuid.uuid4())

            # Initialize conversation
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
                    jd_title=interviewee_data['jd_title']  # Pass jd_title here
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

            # Insert the conversation into MongoDB
            self.mongodb_handler.create_conversation(conversation_data)

            # Add to in-memory cache
            self.conversations[conversation_id] = conversation_data

            # Start conversation with the interviewer
            self.initiate_conversation_with_interviewer(conversation_id)

            # Log the conversation initiation
            logger.info(f"New conversation started: {conversation_id}")
            self.log_conversation_history(conversation_id)

            return conversation_id

        except Exception as e:
            logger.error(f"Error starting conversation: {str(e)}")
            raise


    def initiate_conversation_with_interviewer(self, conversation_id):
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiation.")
            return

        interviewer = conversation['interviewer']
        
        # Extract interviewees who have agreed to take interviews
        interviewees = conversation.get('interviewees', [])
        
        if not interviewees:
            logger.warning(f"No interviewees found for conversation {conversation_id}.")
            candidates_info = "Currently, there are no candidates assigned for interviews."
        else:
            # Log interviewee details for debugging
            for ie in interviewees:
                logger.debug(f"Interviewee: {ie['name']}, JD Title: {ie.get('jd_title', 'N/A')}")
            
            # Create a list of candidate names with their JD titles
            candidates_info_list = [
                f"{interviewee['name']} (JD Title: {interviewee.get('jd_title', 'N/A')})"
                for interviewee in interviewees
            ]
            candidates_info = "Here are the candidates assigned with whom you will be taking an interview:\n" + "\n".join(candidates_info_list)
            logger.info(f"Candidates Info: {candidates_info}")

        system_message = f"""Hello {interviewer['name']}, I’m here to assist with scheduling interviews for the upcoming candidates. Could you please provide your availability for the coming week?

Feel free to share any specific time preferences or constraints you may have. Once I have your availability, I’ll coordinate with the candidates and work to confirm convenient times for each interview.

If there's anything specific you'd like me to keep in mind while setting up the meetings, just let me know! 

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
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for scheduling.")
            return

        interviewer = conversation['interviewer']
        interviewer_slots = interviewer.get('slots', {})

        # Find the interviewee
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)
        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        # Initialize available_slots and archived_slots if not already done
        if not conversation.get('available_slots'):
            conversation['available_slots'] = interviewer_slots.get('time_slots', [])[:]
            self.conversations[conversation_id]['available_slots'] = conversation['available_slots']
            self.mongodb_handler.update_conversation(conversation_id, {
                'available_slots': conversation['available_slots']
            })

        # Prepare the list of slots to offer (excluding those already offered to this interviewee)
        offered_slots = interviewee.get('offered_slots', [])
        slots_to_offer = [slot for slot in conversation['available_slots'] if slot not in offered_slots]

        if not slots_to_offer and conversation.get('archived_slots'):
            # Use archived slots if available
            slots_to_offer = [slot for slot in conversation['archived_slots'] if slot not in offered_slots]

        if not slots_to_offer:
            # No more slots to offer
            asked_for_more_slots = conversation.get('asked_for_more_slots', False)

            if not asked_for_more_slots:
                # Ask interviewer for more slots
                conversation['asked_for_more_slots'] = True
                self.conversations[conversation_id] = conversation
                self.mongodb_handler.update_conversation(conversation_id, {
                    'asked_for_more_slots': True
                })

                # Set interviewee state to NO_SLOTS_AVAILABLE
                interviewee['state'] = ConversationState.NO_SLOTS_AVAILABLE.value
                self.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
                ]
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                # Ask the interviewer for more slots
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

                # Mark interviewer state as AWAITING_MORE_SLOTS_FROM_INTERVIEWER
                interviewer['state'] = ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value
                self.conversations[conversation_id]['interviewer'] = interviewer
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

            else:
                # Interviewer was already asked and presumably denied providing more slots
                # Notify all remaining unscheduled interviewees
                unscheduled_interviewees = [ie for ie in conversation['interviewees']
                                            if ie['state'] not in [ConversationState.SCHEDULED.value, ConversationState.CANCELLED.value]]

                for ie in unscheduled_interviewees:
                    sys_msg = (
                        "We couldn't find any more available slots for your interview. "
                        "We will reach out later if new slots become available."
                    )
                    resp = self.message_handler.generate_response(
                        ie,
                        interviewer,
                        "",
                        sys_msg,
                        conversation_state=ie['state']
                    )
                    self.log_conversation(conversation_id, ie['number'], "system", resp, "AI")
                    self.message_handler.send_message(ie['number'], resp)
                    ie['state'] = ConversationState.NO_SLOTS_AVAILABLE.value

                # Update interviewees in DB
                self.conversations[conversation_id]['interviewees'] = conversation['interviewees']
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                # Notify interviewer
                sys_msg = "No additional slots provided. We have informed the remaining interviewees that no slots are available."
                resp = self.message_handler.generate_response(
                    interviewer,
                    None,
                    "",
                    sys_msg,
                    conversation_state=interviewer['state']
                )
                self.log_conversation(conversation_id, 'interviewer', "system", resp, "AI")
                self.message_handler.send_message(interviewer['number'], resp)

                # Notify role_to_contact_person
                self.notify_contact_person(conversation_id, None, AttentionFlag.NO_AVAILABLE_SLOTS)

            return

        # If we have slots_to_offer
        proposed_slot = slots_to_offer[0]
        if 'offered_slots' not in interviewee:
            interviewee['offered_slots'] = []
        interviewee['offered_slots'].append(proposed_slot)
        self.conversations[conversation_id]['interviewees'] = [
            ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
        ]
        self.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees']
        })

        interviewee['proposed_slot'] = proposed_slot
        interviewee['state'] = ConversationState.CONFIRMATION_PENDING.value
        self.conversations[conversation_id]['interviewees'] = [
            ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
        ]
        self.mongodb_handler.update_conversation(conversation_id, {
            'interviewees': conversation['interviewees']
        })

        # Localize meeting time to the interviewee's timezone
        interviewee_timezone = interviewee.get('timezone', 'UTC')
        meeting_time_utc = datetime.fromisoformat(proposed_slot['start_time'])
        localized_meeting_time = meeting_time_utc.astimezone(pytz.timezone(interviewee_timezone))

        system_message = f"A proposed meeting time has been identified: {localized_meeting_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}. Please confirm if this works for you."

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
        """Finalize the scheduling after interviewee has confirmed."""
        conversation = self.conversations.get(conversation_id)
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

            # Store the scheduled slot and update conversation state
            interviewee['scheduled_slot'] = interviewee['proposed_slot']
            interviewee['state'] = ConversationState.SCHEDULED.value

            # Remove the scheduled slot from available_slots
            if interviewee['proposed_slot'] in conversation['available_slots']:
                conversation['available_slots'].remove(interviewee['proposed_slot'])
                self.conversations[conversation_id]['available_slots'] = conversation['available_slots']
                self.mongodb_handler.update_conversation(conversation_id, {
                    'available_slots': conversation['available_slots']
                })

            self.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            # Send confirmation messages to both interviewer and interviewee
            for participant in [interviewer, interviewee]:
                try:
                    # Default to UTC if timezone is None or invalid
                    participant_timezone = participant.get('timezone', 'UTC')
                    if not participant_timezone:
                        participant_timezone = 'UTC'
                        logger.warning(f"No timezone set for participant {participant['number']}, defaulting to UTC")
                    
                    try:
                        tz = pytz.timezone(participant_timezone)
                    except pytz.exceptions.UnknownTimeZoneError:
                        logger.warning(f"Invalid timezone {participant_timezone} for participant {participant['number']}, defaulting to UTC")
                        tz = pytz.UTC
                    
                    localized_meeting_time = meeting_time_utc.astimezone(tz)
                    system_message = f"Your meeting has been scheduled for {localized_meeting_time.strftime('%A, %B %d, %Y at %I:%M %p %Z')}."

                    response = self.message_handler.generate_response(
                        participant,
                        interviewer if participant['role'] == 'interviewee' else interviewee,
                        "",
                        system_message,
                        conversation_state=participant['state']
                    )
                    self.log_conversation(conversation_id, participant['number'], "system", response, "AI")
                    self.message_handler.send_message(participant['number'], response)
                
                except Exception as e:
                    logger.error(f"Error sending confirmation to participant {participant['number']}: {str(e)}")
                    # Continue with other participant if one fails

            # Clear confirmation flags
            interviewee['confirmed'] = False
            interviewee['proposed_slot'] = None
            self.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            # Add the scheduled slot to scheduled_slots
            if 'scheduled_slots' not in conversation:
                conversation['scheduled_slots'] = []
            conversation['scheduled_slots'].append(interviewee['scheduled_slot'])
            self.conversations[conversation_id]['scheduled_slots'] = conversation['scheduled_slots']
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
                self.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
                ]
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                logger.info(f"Event created for conversation {conversation_id} and interviewee {interviewee_number}.")
            else:
                logger.error(f"Failed to create event for conversation {conversation_id} and interviewee {interviewee_number}.")

            # Initiate next interviewee if available
            self.initiate_next_interviewee(conversation_id)

        except Exception as e:
            logger.error(f"Error finalizing scheduling for interviewee {interviewee_number} in conversation {conversation_id}: {str(e)}")
            logger.error(traceback.format_exc())

    def initiate_next_interviewee(self, conversation_id):
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for initiating next interviewee.")
            return

        # Find the next interviewee who hasn't been scheduled and hasn't been contacted yet
        for interviewee in conversation['interviewees']:
            if interviewee['state'] == ConversationState.AWAITING_AVAILABILITY.value:
                self.message_handler.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])
                return

        logger.info(f"All interviewees have been contacted or scheduled for conversation {conversation_id}.")

    def log_conversation(self, conversation_id: str, participant_id: str, message_type: str, message: str, sender: str) -> None:
        try:
            conversation = self.conversations.get(conversation_id)
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
            # Update participant's conversation history in the database
            if participant_id == 'interviewer':
                self.mongodb_handler.update_conversation(conversation_id, {
                    f'interviewer.conversation_history': participant_history
                })
                self.conversations[conversation_id]['interviewer']['conversation_history'] = participant_history
            else:
                for idx, ie in enumerate(conversation['interviewees']):
                    if ie['number'] == participant_id:
                        self.conversations[conversation_id]['interviewees'][idx]['conversation_history'] = participant_history
                        break
                self.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
            logger.debug(f"Logged message for participant {participant_id} in conversation {conversation_id}: {log_entry}")
        except Exception as e:
            logger.error(f"Error logging conversation: {str(e)}")



scheduler = InterviewScheduler()
