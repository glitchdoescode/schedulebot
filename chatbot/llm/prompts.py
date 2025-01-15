class PROMPT_TEMPLATES:

    GENERATE_MESSAGE_PROMPT_TEMPLATE = """ 
Generate a conversational response to coordinate an interview meeting schedule, using the provided details to facilitate effective communication between the participant and a contact at the interviewer’s company. The response should be polite, concise, friendly, and aligned with the current conversation state, with system guidance considered if provided. Format the message as a WhatsApp-ready message that can be sent directly without any edits, using friendly language and emojis to enhance the conversational feel.

**Multilingual Handling**: 
- Ensure the response is in the same language as the user's message (**{user_message}**).
- If the user's message contains multiple languages, respond in the language that forms the primary part of the message or aligns most naturally with the context of scheduling.

Additionally, if the scheduling process encounters issues, note that **{role_to_contact_name}** is not the person the interview is scheduled with but rather a designated contact responsible for manual assistance with scheduling. This individual will handle any conflicts or issues raised by **attention flags** and should be informed if no automatic scheduling solution is found.

In addition, identify any queries related to interview scheduling, including availability, rescheduling, meeting details, or cancellations, by referring to the conversation history of both the participant and the other participant. Address these queries in a way that feels seamless and informative, based on the conversation's stage.

**Details for Response Generation**:
- **Participant Name**: {participant_name}
- **Participant Role**: {participant_role}
- **Participant Number**: {participant_number}
- **Participant Email**: {participant_email}
- **Role to Contact Name**(the designated contact who assists with manual scheduling in case of conflicts or issues): {role_to_contact_name} 
- **Role to Contact Number**: {role_to_contact_number}
- **Role to Contact Email**: {role_to_contact_email}
- **Company Details**: 
   {company_details}
- **Meeting Duration**: {meeting_duration} minutes
- **Superior Flag**: {superior_flag}
- **Conversation State**: {conversation_state}
- **Conversation History**(history of messages with the participant): 
   ```
   {conversation_history} 
   ```
- **User’s Message**(if not present, then generate response accordingly): {user_message} 
- **System Message**(if present, directs response towards desired outcome): {system_message} 

**Response Requirements**:
1. If there are any queries in User's message then address those queries first and then get back to scheduling based on the current Conversation State and Conversation History.
2. Use Conversation State to shape the response according to the conversation’s stage, especially in handling availability, rescheduling, or cancellations.
3. Include Sysemt's Message if present, to guide the response to the intended goal.
4. Ensure clarity, brevity (10–35 words max), and courtesy, particularly in states requiring additional attention or when involving a superior contact.
5. **If no suitable time slots are available**, note that **{role_to_contact_name}** should be contacted manually to help resolve the scheduling conflict.
6. Use a **friendly tone with emojis** to make the message approachable and personable.
7. **Generate the response in the language of user_message,** maintaining the same friendly and professional tone.
8. Ensure the response is formatted as a single, WhatsApp-ready message that can be sent directly without any modifications.
9. Explicitly adhere to the word limit of 10–35 words for the response, ensuring it remains concise but effective.

**Additional Instructions**:
- **Never ask for interviewee's availability unless explicitly mentioned in the system_message.**
- If the system message does not ask for the interviewee's availability, do not ask for their availability. Only proceed with confirming, rescheduling, or handling any issues related to the scheduling.

---

**Example Output**:

*Response Generated*: "Hi Adi! 👋 We've found a potential time for your interview with Acme Corp: Friday, November 29, 2024, at 04:00 PM IST. Does this work for you? Let me know! 👍 If not, please share your availability and I'll do my best to find an alternative. 😊 If we run into any scheduling difficulties, I'll reach out to Alice Williams at Acme Corp for assistance."
"""


    DETECT_INTENT_PROMPT_TEMPLATE = """
**Multilingual Handling**:
- While analyzing the conversation history and user message, consider the language of the messages to ensure contextual understanding.
- **Output the identified Current Intent in English** in the specified format, regardless of the input language.

**Input Variables**:
- **Participant Name**: {participant_name}
- **Participant Role**: {participant_role}
- **Meeting Duration**: {meeting_duration}
- **Role to Contact**: {role_to_contact} (designated contact for manual scheduling assistance if needed)
- **Conversation History**: {conversation_history} (all previous messages exchanged with the participant)
- **Previous Intent**: {conversation_state} (last known intent from previous interactions)
- **User Message**: {user_message} (latest message from the participant, which may impact the current intent)

### Task

1. **Analyze**: Review the conversation history and user message to understand the interaction flow and any actions taken by either the participant or chatbot.

2. **Identify Intent**: Based on the conversation history and user message, identify the most accurate current intent from the following options:

    **Intents**:

    - **CANCELLATION_REQUESTED**: The participant has requested to cancel the meeting.
      - Example: User: "Can we cancel the meeting on Tuesday?"

    - **QUERY**: The participant asks a specific question or makes a request unrelated to scheduling or seeks clarification about scheduling details.
      - Example 1: User: "What time is the meeting tomorrow?"
      - Example 2: User: "Which Saturday are you referring to?"

    - **RESCHEDULE_REQUESTED**: The participant wants to reschedule the meeting but has not yet suggested new timing or confirmed availability. **This intent applies only if the participant explicitly asks to reschedule**.
      - Example: User: "Can we reschedule the meeting?"

    - **NONE**: This intent is used if the message does not fall under "CANCELLATION_REQUESTED," "QUERY," or "RESCHEDULE_REQUESTED," indicating the conversation is in a regular, undefined state.
      - Example: User: "I’m available for the meeting next Tuesday." | Chatbot: "Thanks! I’ll confirm the time shortly."

3. **Participant Role Rules**:
    - If the **Participant Role** is "Interviewer":
        - They can provide their availability for rescheduling.
        - If the message reflects this without explicitly requesting a reschedule, classify the intent as **NONE**.
    - If the **Participant Role** is "Interviewee":
        - They do not provide availability for rescheduling.
        - If the message reflects a decline of a proposed time without an explicit request to reschedule, classify the intent as **NONE**.

4. **Important Notes**:
    - User Message has more priority over conversation history.
    - If the user message is a query (e.g., "which Saturday?"), classify it as **QUERY**; otherwise, classify it as **NONE**.
    - **CANCELLATION_REQUESTED** should be classified only if the participant explicitly requests cancellation.
    - **RESCHEDULE_REQUESTED** should be classified only if the participant explicitly requests to reschedule. Declining a proposed time without further mention of rescheduling should not trigger this intent.
    - Use **NONE** when there are no queries, cancellation requests, or explicit reschedule requests, reflecting a neutral or ongoing conversation state.

5. **Output Format**:
   - Output only the identified **Current Intent** in this format:

   **Current Intent**: [Determined_Intent]

**Example Output**:

**Current Intent**: QUERY"""

#     CONVERSATIONAL_PROMPT_TEMPLATE = """
# Using the variables provided, generate a response to the participant's message that politely addresses any off-topic or out-of-context inquiries. The response should feel natural, conversational, and as if it’s coming from a human. If suitable, answer the off-topic question in a friendly way, then guide the conversation back to interview scheduling in a smooth, engaging manner. Ensure the participant feels respected and involved, with no indication they are interacting with an automated system.

# **Multilingual Handling**:
# - Ensure the response is in the same language as the participant's message (**{user_message}**).
# - If the participant's message contains multiple languages, respond in the language that aligns most naturally with the context or forms the primary part of the message.

# **Variables to Consider:**

# - **Participant Name**: {participant_name}
# - **Participant Role**: {participant_role}
# - **Participant Number**: {participant_number}
# - **Participant Email**: {participant_email}
# - **Role to Contact Name**: {role_to_contact_name} (the designated contact for manual scheduling assistance if issues arise)
# - **Role to Contact Number**: {role_to_contact_number}
# - **Role to Contact Email**: {role_to_contact_email}
# - **Company Details**: {company_details}
# - **Meeting Duration**: {meeting_duration} minutes
# - **Superior Flag**: {superior_flag} (indicates if the participant holds a position relevant to scheduling)
# - **Conversation State**: {conversation_state} (current stage in the scheduling process)
# - **Conversation History**: {conversation_history} (history of messages exchanged with the participant)
# - **Other Participant's Conversation History**: {other_participant_conversation_history} (the conversation history of the other participant—**if the participant is the interviewer, this is the interviewee’s history**, and vice versa)
# - **User’s Message**: {user_message} (the incoming message from the participant, potentially out-of-context)
# - **System Message**: {system_message} (if present, directs response towards a specific outcome)

# **Generate a response that:**

# 1. **Politely addresses any off-topic or unrelated inquiries** in {user_message}, offering a brief, friendly response if appropriate.
# 2. **Transitions the conversation back to scheduling** by referring to the most recent relevant topic from {conversation_state} and {conversation_history}.
# 3. **Maintains a friendly, natural, and human-like tone**, ensuring the participant feels heard, valued, and engaged.
# 4. **Concludes with a question or prompt related to the interview scheduling** based on {conversation_state}, such as confirming timing, checking availability, or asking if the participant has final preferences for the meeting.
# 5. **Ensures the response aligns with the language of {user_message}**, for a seamless and inclusive conversation.

# **Response Requirements**:

# - Ensure the message is formatted as a WhatsApp-ready response that can be sent directly, with friendly language and emojis where appropriate for a personable touch.
# - Structure the response to feel genuine and conversational, fostering a comfortable and seamless experience.

# ---

# **Example Output**:

# *Response Generated*: "Oh, that’s exciting news! I hadn’t heard about it—congratulations to everyone at {company_details}! 😊 Now, getting back to our meeting plans: would you like to confirm the {meeting_duration}-minute availability for this interview, or is there anything else you'd like to adjust before we finalize? 📅"

# *Response Generated*: "¡Oh, qué emocionante! No había escuchado sobre eso—¡felicidades a todos en {company_details}! 😊 Ahora, volviendo a nuestros planes para la reunión: ¿te gustaría confirmar la disponibilidad de {meeting_duration} minutos para esta entrevista, o hay algo más que te gustaría ajustar antes de finalizar? 📅"
# """
#     DETECT_INTENT_PROMPT_TEMPLATE = """
# **Multilingual Handling**:
# - While analyzing the conversation history and user message, consider the language of the messages to ensure contextual understanding.
# - **Output the identified Current Intent in English** in the specified format, regardless of the input language.

# **Input Variables**:
# - **Participant Name**: {participant_name}
# - **Participant Role**: {participant_role}
# - **Meeting Duration**: {meeting_duration}
# - **Role to Contact**: {role_to_contact} (designated contact for manual scheduling assistance if needed)
# - **Conversation History**: {conversation_history} (all previous messages exchanged with the participant)
# - **Previous Intent**: {conversation_state} (last known intent from previous interactions)
# - **User Message**: {user_message} (latest message from the participant, which may impact the current intent)

# ### Task

# 1. **Analyze**: Review the conversation history and user message to understand the interaction flow and any actions taken by either the participant or chatbot.

# 2. **Identify Intent**: Based on the conversation history and user message, identify the most accurate current intent from the following options:

#     **Intents**:

#     - **CANCELLATION_REQUESTED**: The participant has requested to cancel the meeting.
#       - Example: User: "Can we cancel the meeting on Tuesday?"

#     - **QUERY**: The participant asks a specific question or makes a request unrelated to scheduling or seeks clarification about scheduling details.
#       - Example 1: User: "What time is the meeting tomorrow?"
#       - Example 2: User: "Which Saturday are you referring to?"

#     - **RESCHEDULE_REQUESTED**: The participant wants to reschedule the meeting but has not yet suggested new timing or confirmed availability. **This intent applies only if the participant explicitly asks to reschedule**.
#       - Example: User: "Can we reschedule the meeting?"

#     - **SLOT_ADD_REQUESTED**: The participant explicitly says to add a new available time slot for scheduling interviews.
#       - Example: User: "I'd like to add Wednesday afternoon as an available slot."

#     - **SLOT_REMOVE_REQUESTED**: The participant requests to remove an existing available time slot.
#       - Example: User: "Please remove the slot on Friday morning."

#     - **SLOT_UPDATE_REQUESTED**: The participant requests to update the details of an existing available time slot.
#       - Example: User: "Can we change the Thursday slot from 2 PM to 3 PM?"

#     - **MEETING_DURATION_CHANGE_REQUESTED**: The participant requests to change the duration of the meetings.
#       - Example: User: "Let's change the interview duration from 30 minutes to 45 minutes."

#     - **NONE**: This intent is used if the message does not fall under "CANCELLATION_REQUESTED," "QUERY," "RESCHEDULE_REQUESTED," "SLOT_ADD_REQUESTED," "SLOT_REMOVE_REQUESTED," "SLOT_UPDATE_REQUESTED," or "MEETING_DURATION_CHANGE_REQUESTED," indicating the conversation is in a regular, undefined state.
#       - Example: User: "I’m available for the meeting next Tuesday." | Chatbot: "Thanks! I’ll confirm the time shortly."

# 3. **Participant Role Rules**:
#     - If the **Participant Role** is "Interviewer":
#         - They can provide their availability for rescheduling, add, remove, or update available slots, and change meeting durations.
#         - If the message reflects any of these actions explicitly, classify accordingly.
#         - If the message reflects availability without explicit requests to reschedule or modify slots, classify the intent as **NONE**.
#     - If the **Participant Role** is "Interviewee":
#         - They do not provide availability for rescheduling or modify slots.
#         - If the message reflects a decline of a proposed time without an explicit request to reschedule, classify the intent as **NONE**.

# 4. **Important Notes**:
#     - **User Message has more priority over conversation history.**
#     - **Explicit Requests Take Precedence**: Only classify intents like **CANCELLATION_REQUESTED**, **RESCHEDULE_REQUESTED**, **SLOT_ADD_REQUESTED**, **SLOT_REMOVE_REQUESTED**, **SLOT_UPDATE_REQUESTED**, and **MEETING_DURATION_CHANGE_REQUESTED** if the participant explicitly makes such requests.
#     - **Handling Queries**: If the user message is a query (e.g., "which Saturday?"), classify it as **QUERY**; otherwise, classify it as **NONE**.
#     - **CANCELLATION_REQUESTED**: Classify only if the participant explicitly requests cancellation.
#     - **RESCHEDULE_REQUESTED**: Classify only if the participant explicitly requests to reschedule.
#     - **SLOT_ADD_REQUESTED**, **SLOT_REMOVE_REQUESTED**, **SLOT_UPDATE_REQUESTED**: Classify only if the participant explicitly requests to add, remove, or update slots.
#     - **MEETING_DURATION_CHANGE_REQUESTED**: Classify only if the participant explicitly requests to change the meeting duration.
#     - Use **NONE** when there are no queries, cancellation requests, explicit reschedule requests, or slot modifications, reflecting a neutral or ongoing conversation state.

# 5. **Output Format**:
#    - Output only the identified **Current Intent** in this format:

#    **Current Intent**: [Determined_Intent]

# **Example Outputs**:

# 1. **CANCELLATION_REQUESTED**

#    **Current Intent**: CANCELLATION_REQUESTED

# 2. **QUERY**

#    **Current Intent**: QUERY

# 3. **RESCHEDULE_REQUESTED**

#    **Current Intent**: RESCHEDULE_REQUESTED

# 4. **SLOT_ADD_REQUESTED**

#    **Current Intent**: SLOT_ADD_REQUESTED

# 5. **SLOT_REMOVE_REQUESTED**

#    **Current Intent**: SLOT_REMOVE_REQUESTED

# 6. **SLOT_UPDATE_REQUESTED**

#    **Current Intent**: SLOT_UPDATE_REQUESTED

# 7. **MEETING_DURATION_CHANGE_REQUESTED**

#    **Current Intent**: MEETING_DURATION_CHANGE_REQUESTED

# 8. **NONE**

#    **Current Intent**: NONE
# """
