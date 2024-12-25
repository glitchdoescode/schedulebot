#chatbot/constants.py

from enum import Enum

class AttentionFlag(Enum):
    NO_RESPONSE = "NO_RESPONSE"
    MISSED_SCHEDULED_MEETING = "MISSED_SCHEDULED_MEETING"
    NO_AVAILABLE_SLOTS = "NO_AVAILABLE_SLOTS"

class ConversationState(Enum):
    AWAITING_AVAILABILITY = 'awaiting_availability'
    AWAITING_CANCELLATION_INTERVIEWEE_NAME = 'awaiting_cancellation_interviewee_name'
    AWAITING_SLOT_CONFIRMATION= 'awaiting_slot_confirmation'
    CONFIRMATION_PENDING = 'confirmation_pending'
    NO_SLOTS_AVAILABLE = 'no_slots_available'
    SCHEDULED = 'scheduled'
    CANCELLED = 'cancelled'
    QUERY = 'query'
    REMINDER_SENT = 'reminder_sent'
    NO_RESPONSE = 'no_response_state'
    CONVERSATION_ACTIVE = 'conversation_active'
    TIMEZONE_CLARIFICATION = 'timezone_clarification'
    COMPLETED = 'completed'
    AWAITING_MORE_SLOTS_FROM_INTERVIEWER = 'awaiting_more_slots_from_interviewer'
