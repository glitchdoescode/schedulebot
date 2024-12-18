from datetime import datetime, timedelta
import pytz
from typing import List, Dict
from chatbot.constants import AttentionFlag

class AttentionFlagManager:
    def __init__(self):
        self.flags = {}  # conversation_id -> {participant_id -> [flags]}
        self.response_timeouts = {}  # conversation_id -> {participant_id -> last_response_time}
        self.RESPONSE_THRESHOLD = timedelta(hours=24)
        self.RESCHEDULE_THRESHOLD = 3
        self.CANCELLATION_THRESHOLD = 2
        self.OUT_OF_CONTEXT_THRESHOLD = 5

    def add_flag(self, conversation_id: str, participant_id: str, flag_type: AttentionFlag):
        if conversation_id not in self.flags:
            self.flags[conversation_id] = {}
        if participant_id not in self.flags[conversation_id]:
            self.flags[conversation_id][participant_id] = []
        self.flags[conversation_id][participant_id].append({
            'type': flag_type,
            'timestamp': datetime.now(pytz.UTC),
            'resolved': False
        })

    def resolve_flag(self, conversation_id: str, participant_id: str, flag_type: AttentionFlag):
        if conversation_id in self.flags and participant_id in self.flags[conversation_id]:
            for flag in self.flags[conversation_id][participant_id]:
                if flag['type'] == flag_type and not flag['resolved']:
                    flag['resolved'] = True
                    flag['resolved_at'] = datetime.now(pytz.UTC)

    def get_active_flags(self, conversation_id: str, participant_id: str) -> List[Dict]:
        if conversation_id in self.flags and participant_id in self.flags[conversation_id]:
            return [flag for flag in self.flags[conversation_id][participant_id] if not flag['resolved']]
        return []

    def update_last_response(self, conversation_id: str, participant_id: str):
        if conversation_id not in self.response_timeouts:
            self.response_timeouts[conversation_id] = {}
        self.response_timeouts[conversation_id][participant_id] = datetime.now(pytz.UTC)
        # Resolve any existing no-response flags
        self.resolve_flag(conversation_id, participant_id, AttentionFlag.NO_RESPONSE)
