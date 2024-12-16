// frontend/src/types/api.ts
export interface Interviewee {
    name: string;
    number: string;
    email: string;
    jd_title: string;
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
    interviewees: Array<{
      id: string;
      name: string;
      email: string;
      number: string;
      status: string;
    }>;
    status: string;
    last_activity: string;
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
    status: string;
  }
  
  export interface AttentionFlag {
    id: string;
    conversation_id: string;
    message: string;
    severity: 'low' | 'medium' | 'high';
    created_at: string;
    resolved: boolean;
  }