// frontend/src/types/api.ts

export interface Interviewee {
  id: string;
  name: string;
  number: string;
  email: string;
  jd_title: string;
  status: string; // e.g., 'scheduled', 'pending', 'cancelled'
}

export interface Conversation {
  interviewer_name: string;
  interviewer_number: string;
  interviewer_email: string;
  superior_flag: string;
  meeting_duration: number;
  interviewees: Interviewee[];
  role_to_contact_name: string;
  role_to_contact_number: string;
  role_to_contact_email: string;
  company_details: string;
}

export interface InitializeResponse {
  results: Array<{
    index: number;
    status: 'success' | 'failed';
    conversation_id?: string;
    error?: string;
  }>;
}

export interface ActiveConversation {
  id: string;
  interviewer_name: string;
  interviewer_email: string;
  interviewer_number: string;
  interviewees: Interviewee[];
  status: 'active' | 'completed' | 'queued';
  last_activity: string;
  completed_at?: string; // Optional field for completed conversations
  attention_flags?: AttentionFlag[]; // Optional field for associated attention flags
}

export interface ScheduledInterview {
  id: string;
  title: string;
  interviewer_name: string;
  interviewer_email: string;
  interviewer_number: string;
  interviewee_name: string;
  interviewee_email: string;
  interviewee_number: string;
  scheduled_time: string;
  status: string; // e.g., 'scheduled'
}

export interface AttentionFlag {
  id: string;
  conversation_id: string;
  message: string;
  severity: 'low' | 'medium' | 'high';
  created_at: string;
  resolved: boolean;
}
