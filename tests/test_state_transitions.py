import unittest
from datetime import datetime
from chatbot.conversation import DebuggableInterviewScheduler
from chatbot.constants import ConversationState

class TestConversationStateTransitions(unittest.TestCase):
    def setUp(self):
        # Create an instance of DebuggableInterviewScheduler
        self.scheduler = DebuggableInterviewScheduler()
        
        # Create a sample conversation
        self.conversation_id = "interviewer123_interviewee456"
        self.scheduler.conversations[self.conversation_id] = {
            'interviewer': {
                'name': 'Alice',
                'number': '1234567890',
                'email': 'alice@example.com',
                'role': 'interviewer',
                'state': ConversationState.INITIAL,
                'conversation_history': []
            },
            'interviewee': {
                'name': 'Bob',
                'number': '0987654321',
                'email': 'bob@example.com',
                'role': 'interviewee',
                'state': ConversationState.INITIAL,
                'conversation_history': []
            }
        }

    def test_initial_to_awaiting_availability(self):
        # Transition from INITIAL to AWAITING_AVAILABILITY for interviewer
        self.scheduler.update_state(self.conversation_id, 'interviewer', ConversationState.AWAITING_AVAILABILITY)
        self.assertEqual(self.scheduler.conversations[self.conversation_id]['interviewer']['state'], ConversationState.AWAITING_AVAILABILITY)
        
        # Verify the state transition log
        transition_log = self.scheduler.state_transitions_log[-1]
        self.assertEqual(transition_log['old_state'], ConversationState.INITIAL.value)
        self.assertEqual(transition_log['new_state'], ConversationState.AWAITING_AVAILABILITY.value)

    def test_awaiting_availability_to_scheduled(self):
        # Set initial state to AWAITING_AVAILABILITY
        self.scheduler.conversations[self.conversation_id]['interviewee']['state'] = ConversationState.AWAITING_AVAILABILITY
        
        # Transition to SCHEDULED
        self.scheduler.update_state(self.conversation_id, 'interviewee', ConversationState.SCHEDULED)
        self.assertEqual(self.scheduler.conversations[self.conversation_id]['interviewee']['state'], ConversationState.SCHEDULED)
        
        # Verify the state transition log
        transition_log = self.scheduler.state_transitions_log[-1]
        self.assertEqual(transition_log['old_state'], ConversationState.AWAITING_AVAILABILITY.value)
        self.assertEqual(transition_log['new_state'], ConversationState.SCHEDULED.value)

    def test_state_transition_logging(self):
        # Test multiple state transitions and check the log
        self.scheduler.update_state(self.conversation_id, 'interviewer', ConversationState.AWAITING_AVAILABILITY)
        self.scheduler.update_state(self.conversation_id, 'interviewer', ConversationState.SCHEDULED)
        
        # Verify the log contains both transitions
        self.assertEqual(len(self.scheduler.state_transitions_log), 2)
        self.assertEqual(self.scheduler.state_transitions_log[0]['new_state'], ConversationState.AWAITING_AVAILABILITY.value)
        self.assertEqual(self.scheduler.state_transitions_log[1]['new_state'], ConversationState.SCHEDULED.value)

    def test_no_transition_if_same_state(self):
        # Set initial state to SCHEDULED
        self.scheduler.conversations[self.conversation_id]['interviewer']['state'] = ConversationState.SCHEDULED
        
        # Attempt to update to the same state
        self.scheduler.update_state(self.conversation_id, 'interviewer', ConversationState.SCHEDULED)
        
        # Verify no new log entry is created
        self.assertEqual(len(self.scheduler.state_transitions_log), 0)

if __name__ == "__main__":
    unittest.main()
