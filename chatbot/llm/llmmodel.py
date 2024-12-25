# chatbot/llm/llmmodel.py
from langchain.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
import logging
import getpass
import os
import json
import re
from chatbot.llm.prompts import PROMPT_TEMPLATES
from datetime import datetime

# Load environment variables
load_dotenv()

if "GOOGLE_API_KEY" not in os.environ:
    os.environ["GOOGLE_API_KEY"] = getpass.getpass("Enter your Google API key: ")

# Configure logging
logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

class LLMModel:
    def generate_message(self, participant_name, participant_number, participant_email, participant_role, superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number, role_to_contact_email, company_details, conversation_history, conversation_state, user_message, system_message):
        # Guardrail: Validate user input before invoking the LLM
        correction_message = self.correct_user_input_with_nlp(user_message)
        if correction_message:
            logger.info(f"Sending correction to user: {correction_message}")
            return correction_message  # Return the corrective message directly

        # Proceed with LLM generation if no correction is needed
        PROMPT_TEMPLATE = f"""{PROMPT_TEMPLATES.GENERATE_MESSAGE_PROMPT_TEMPLATE}"""
        
        llm_model = ChatGoogleGenerativeAI(
            model="gemini-1.5-pro",
            temperature=1,
        )

        prompt_template = PromptTemplate(
            input_variables=[
                'participant_name',
                'participant_number',
                'participant_email',
                'participant_role',
                'superior_flag',
                'meeting_duration',
                'role_to_contact_name',
                'role_to_contact_number',
                'role_to_contact_email',
                'company_details',
                'conversation_history',
                'conversation_state',
                'user_message',
                'system_message'
            ],
            template=PROMPT_TEMPLATE
        )

        chain = prompt_template | llm_model

        response = chain.invoke({
            'participant_name': participant_name,
            'participant_number': participant_number,
            'participant_email': participant_email,
            'participant_role': participant_role,
            'superior_flag': superior_flag,
            'meeting_duration': meeting_duration,
            'role_to_contact_name': role_to_contact_name,
            'role_to_contact_number': role_to_contact_number,
            'role_to_contact_email': role_to_contact_email,
            'company_details': company_details,
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message,
            'system_message': system_message
        })
        
        logger.info(f"Generated message response: {response.content}")
        return response.content
    
    def answer_query(self, participant_name, participant_role, meeting_duration, role_to_contact_name, conversation_history, conversation_state, user_message, **kwargs):
        PROMPT_TEMPLATE = """
You are an AI assistant helping with scheduling meetings. The participant has asked a question. Based on the conversation history and the user's message, provide a clear and helpful answer.

Participant Name: {participant_name}
Participant Role: {participant_role}
Meeting Duration: {meeting_duration}
Role to Contact: {role_to_contact_name}
Conversation History: 
```
{conversation_history}
```
Conversation State: {conversation_state}
User Message: {user_message}

Answer the participant's question in a professional and concise manner.
"""

        llm_model = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            temperature=0.7,
        )

        prompt_template = PromptTemplate(
            input_variables=[
                'participant_name',
                'participant_role',
                'meeting_duration',
                'role_to_contact_name',
                'conversation_history',
                'conversation_state',
                'user_message'
            ],
            template=PROMPT_TEMPLATE
        )

        chain = prompt_template | llm_model

        response = chain.invoke({
            'participant_name': participant_name,
            'participant_role': participant_role,
            'meeting_duration': meeting_duration,
            'role_to_contact_name': role_to_contact_name,
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message
        })

        logger.info(f"Generated answer to query: {response.content}")
        return response.content

    def correct_user_input_with_nlp(self, user_message):
        """
        Correct user input if it contains logical errors (e.g., mismatched holiday dates).

        Args:
            user_message (str): The user's natural language message.

        Returns:
            str: A corrective message for the user if needed, otherwise an empty string.
        """
        try:
            # Use Gemini Flash to parse the user message
            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.5,
            )

            nlp_prompt = PromptTemplate(
                input_variables=['user_message'],
                template="""
                    Extract the following structured information from the message:
                    - Mentioned event names (if any)
                    - Event dates in YYYY-MM-DD format
                    - Event times in HH:MM format

                    Example input:
                    "I want to schedule a meeting on Christmas 1 pm on 1 Dec."

                    Example output:
                    {{
                        "events": [
                            {{
                                "name": "Christmas",
                                "date": "2024-12-01",
                                "time": "13:00"
                            }}
                        ]
                    }}

                    Message: {user_message}
                """
            )

            chain = nlp_prompt | llm_model

            # Invoke the NLP model to parse the user message
            response = chain.invoke({'user_message': user_message})
            llm_output = response.content.strip()

            logger.info(f"{llm_output}")

            # Check if the response is empty
            if not llm_output:
                logger.warning("LLM output is empty. Proceeding without correction.")
                return ""

            # Validate and sanitize the JSON output
            parsed_data = self.sanitize_and_parse_json(llm_output)

            if not parsed_data:
                logger.warning("LLM output could not be parsed. Proceeding without correction.")
                return ""

            # Check and correct the extracted data
            return self.correct_parsed_data(parsed_data)
        except Exception as e:
            logger.error(f"Error in NLP correction: {str(e)}")
            # Proceed without correction if an error occurs
            return ""

    def sanitize_and_parse_json(self, llm_output):
        """
        Sanitize and parse the LLM output to ensure it's valid JSON.

        Args:
            llm_output (str): Raw output from the LLM.

        Returns:
            dict: Parsed JSON data if valid, otherwise None.
        """
        try:
            # Remove unwanted characters like backticks and extra spaces
            llm_output_cleaned = (
                llm_output.strip()
                .replace("```json", "")  # Remove opening JSON formatting markers
                .replace("```", "")     # Remove closing backticks
                .strip()
            )

            # Attempt to parse the cleaned JSON
            return json.loads(llm_output_cleaned)
        except json.JSONDecodeError:
            logger.error(f"Sanitization failed. Unable to parse LLM output: {llm_output}")
            return None


    def correct_parsed_data(self, parsed_data):
        """
        Generate a corrective message if extracted dates do not match known holidays.
        If no holiday is mentioned, return an empty string.

        Args:
            parsed_data (dict): Structured data extracted by the NLP model.

        Returns:
            str: A corrective message if needed, otherwise an empty string.
        """
        holidays = {
            "Christmas": datetime(2024, 12, 25).date(),  # Ensure this is a datetime.date object
            "New Year": datetime(2025, 1, 1).date(),
        }

        corrections = []

        # Check if any extracted events match known holidays
        for event in parsed_data.get("events", []):
            event_name = event.get("name")
            event_date_str = event.get("date")  # Date as a string from JSON
            event_time = event.get("time")

            if event_name in holidays:
                expected_date = holidays[event_name]
                try:
                    # Convert event_date_str to a datetime.date object
                    event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()

                    if event_date != expected_date:
                        corrections.append(
                            f"It seems like you mentioned {event_name} on {event_date_str} at {event_time}, "
                            f"but {event_name} is actually on {expected_date.strftime('%d %b %Y')}. "
                            f"Do you want to update the date to {expected_date.strftime('%d %b %Y')}?"
                        )
                except ValueError as e:
                    logger.error(f"Invalid date format for event: {event_date_str}. Error: {e}")
                    corrections.append(f"Invalid date provided for {event_name}. Please check the date format.")

            # Return correction if any, else an empty string
        return " ".join(corrections) if corrections else ""
    
    def extract_slot_info(self, user_message, available_slots):
        """
        Extract multiple slot information (start_time and end_time) from the user message.

        Args:
            user_message (str): The user's natural language message.
            available_slots (list): List of current available slots for reference.

        Returns:
            list: A list of dictionaries, each containing 'start_time' and 'end_time'.
        """
        try:
            json_template = """
                [
                    {{
                        "start_time": "YYYY-MM-DDTHH:MM:SS",
                        "end_time": "YYYY-MM-DDTHH:MM:SS"
                    }},
                    {{
                        "start_time": "YYYY-MM-DDTHH:MM:SS",
                        "end_time": "YYYY-MM-DDTHH:MM:SS"
                    }}
                ]
            """
            PROMPT_TEMPLATE = f"""
                Extract all slot information from the following message.

                **Input Message**:
                {user_message}

                **Available Slots for Reference**:
                {available_slots}

                **Output Format (Do not include anything other than JSON array of slots)**:
                {json_template}
            """

            prompt = PromptTemplate(
                input_variables=['user_message'],
                template=PROMPT_TEMPLATE
            )

            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.3,
            )

            chain = prompt | llm_model

            response = chain.invoke({'user_message': user_message})
            llm_output = response.content.strip()

            logger.info(f"Extracted slot info response: {llm_output}")

            # Extract JSON from the response
            parsed_data = self.extract_json_from_response(llm_output)

            if not parsed_data:
                logger.error("Failed to extract slot information from the response.")
                return None

            # Validate each slot in the list
            valid_slots = []
            for slot in parsed_data:
                if 'start_time' not in slot or 'end_time' not in slot:
                    logger.error("One of the extracted slots is incomplete.")
                    continue
                try:
                    datetime.fromisoformat(slot['start_time'])
                    datetime.fromisoformat(slot['end_time'])
                    valid_slots.append(slot)
                except ValueError:
                    logger.error(f"Invalid datetime format in slot: {slot}")
                    continue

            if not valid_slots:
                logger.error("No valid slots were extracted.")
                return None

            return valid_slots

        except Exception as e:
            logger.error(f"Error in extract_slot_info: {str(e)}")
            return None

    def extract_slot_info_for_update(self, user_message, available_slots):
        """
        Extract multiple slot update information (old_start_time and new_start_time) from the user message.

        Args:
            user_message (str): The user's natural language message.
            available_slots (list): List of current available slots for reference.

        Returns:
            list: A list of dictionaries, each containing 'old_start_time' and 'new_start_time'.
        """
        try:
            json_template = """
                [
                    {{
                        "old_start_time": "YYYY-MM-DDTHH:MM:SS",
                        "new_start_time": "YYYY-MM-DDTHH:MM:SS"
                    }},
                    {{
                        "old_start_time": "YYYY-MM-DDTHH:MM:SS",
                        "new_start_time": "YYYY-MM-DDTHH:MM:SS"
                    }}
                ]
            """
            PROMPT_TEMPLATE = f"""
                Extract all slot update information from the following message.

                **Input Message**:
                {user_message}

                **Available Slots for Reference**:
                {available_slots}

                **Output Format (Do not include anything other than JSON array of slot updates)**:
                {json_template}
            """

            prompt = PromptTemplate(
                input_variables=['user_message'],
                template=PROMPT_TEMPLATE
            )

            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.3,
            )

            chain = prompt | llm_model

            response = chain.invoke({'user_message': user_message})
            llm_output = response.content.strip()

            logger.info(f"Extracted slot update info response: {llm_output}")

            # Extract JSON from the response
            parsed_data = self.extract_json_from_response(llm_output)

            if not parsed_data:
                logger.error("Failed to extract slot update information from the response.")
                return None

            # Validate each slot update in the list
            valid_slot_updates = []
            for slot in parsed_data:
                if 'old_start_time' not in slot or 'new_start_time' not in slot:
                    logger.error("One of the extracted slot updates is incomplete.")
                    continue
                try:
                    datetime.fromisoformat(slot['old_start_time'])
                    datetime.fromisoformat(slot['new_start_time'])
                    valid_slot_updates.append(slot)
                except ValueError:
                    logger.error(f"Invalid datetime format in slot update: {slot}")
                    continue

            if not valid_slot_updates:
                logger.error("No valid slot updates were extracted.")
                return None

            return valid_slot_updates

        except Exception as e:
            logger.error(f"Error in extract_slot_info_for_update: {str(e)}")
            return None
    
    # def generate_conversational_message(self, participant_name, participant_number, participant_email, participant_role, superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number, role_to_contact_email, company_details, conversation_history, conversation_state, user_message, system_message, other_participant_conversation_history):
    #     PROMPT_TEMPLATE = f"""{PROMPT_TEMPLATES.CONVERSATIONAL_PROMPT_TEMPLATE}"""
        
    #     llm_model = ChatGoogleGenerativeAI(
    #         model="gemini-1.5-flash",
    #         temperature=1,
    #     )

    #     prompt_template = PromptTemplate(
    #         input_variables=[
    #             'participant_name',
    #             'participant_number',
    #             'participant_email',
    #             'participant_role',
    #             'superior_flag',
    #             'meeting_duration',
    #             'role_to_contact_name',
    #             'role_to_contact_number',
    #             'role_to_contact_email',
    #             'company_details',
    #             'conversation_history',
    #             'conversation_state',
    #             'user_message',
    #             'system_message',
    #             'other_participant_conversation_history'
    #         ],
    #         template=PROMPT_TEMPLATE
    #     )

    #     chain = prompt_template | llm_model

    #     response = chain.invoke({
    #         'participant_name': participant_name,
    #         'participant_number': participant_number,
    #         'participant_email': participant_email,
    #         'participant_role': participant_role,
    #         'superior_flag': superior_flag,
    #         'meeting_duration': meeting_duration,
    #         'role_to_contact_name': role_to_contact_name,
    #         'role_to_contact_number': role_to_contact_number,
    #         'role_to_contact_email': role_to_contact_email,
    #         'company_details': company_details,
    #         'conversation_history': conversation_history,
    #         'conversation_state': conversation_state,
    #         'user_message': user_message,
    #         'system_message': system_message,
    #         'other_participant_conversation_history': other_participant_conversation_history
    #     })
        
    #     logger.info(f"Generated conversational message response: {response.content}")
    #     return response.content

    def detect_intent(self, participant_name, participant_role, meeting_duration, role_to_contact, conversation_history, conversation_state, user_message):
        PROMPT_TEMPLATE = f"""{PROMPT_TEMPLATES.DETECT_INTENT_PROMPT_TEMPLATE}"""
        
        llm_model = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            temperature=0.7,
        )

        prompt_template = PromptTemplate(
            input_variables=[
                'participant_name', 
                'participant_role', 
                'meeting_duration', 
                'role_to_contact', 
                'conversation_history',
                'conversation_state',
                'user_message'
            ],
            template=PROMPT_TEMPLATE
        )

        chain = prompt_template | llm_model

        response = chain.invoke({
            'participant_name': participant_name,
            'participant_role': participant_role,
            'meeting_duration': meeting_duration,
            'role_to_contact': role_to_contact,
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message
        })
        
        logger.info(f"Detected intent response: {response.content}")
        return response.content
    
    def extract_json_from_response(self, response_text):
        """
        Extracts JSON content from a given response text and converts it into a Python dictionary.

        Args:
            response_text (str): The raw text containing the JSON data.

        Returns:
            dict: A dictionary containing the parsed JSON data.
        """
        try:
            # Extract the JSON part using regex
            json_match = re.search(r"```json\n(.*?)\n```", response_text, re.DOTALL)
            if json_match:
                json_content = json_match.group(1)
                # Convert to Python dictionary
                return json.loads(json_content)
            else:
                raise ValueError("No valid JSON block found in the response.")
        except Exception as e:
            print(f"Error extracting JSON: {e}")
            return None

    def detect_confirmation(self, participant_name, participant_role, meeting_duration, conversation_history, conversation_state, user_message):
        
        json="""
            {{
                "confirmed": true/false,
                "reason": "Optional explanation if not confirmed or unclear"
            }}
            """
        
        PROMPT_TEMPLATE = f"""
Using the input variables, determine whether the participant has confirmed or declined the proposed meeting time based on their latest message and conversation history.

**Multilingual Handling**:
- While analyzing the conversation history and user message, consider the language of the messages to ensure contextual understanding.
- **Output the confirmation status in English** in the specified format, regardless of the input language.

**Input Variables:**

- **Participant Name**: {participant_name}
- **Participant Role**: {participant_role}
- **Meeting Duration**: {meeting_duration}
- **Conversation History**: {conversation_history} (all previous messages exchanged with the participant)
- **Conversation State**: {conversation_state} (current stage in the scheduling process)
- **User Message**: {user_message} (latest message from the participant)

### Task

1. **Analyze**: Review the conversation history and user message to understand if the participant has confirmed or declined the proposed meeting time.

2. **Determine Confirmation**:
   - If the participant has **explicitly confirmed** the proposed time (e.g., "Yes, that works for me", "I confirm the meeting", "Sounds good"), mark as **confirmed**.
   - If the participant has **declined** the proposed time (e.g., "I can't make it at that time", "No, that doesn't work"), mark as **not confirmed**.
   - If the response is **ambiguous** or does not provide a clear confirmation or declination, mark as **unclear**.

3. **Output Format**:
   - Output only the confirmation status in JSON format:

   ```json

   {json}

   ```

"""
        
        llm_model = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            temperature=0.7,
        )

        
        prompt_template = PromptTemplate(
            input_variables=[
                'participant_name',
                'participant_role',
                'meeting_duration',
                'conversation_history',
                'conversation_state',
                'user_message'
            ],
            template=PROMPT_TEMPLATE

        )

        
        chain = prompt_template | llm_model

        response = chain.invoke({
            'participant_name': participant_name,
            'participant_role': participant_role,
            'meeting_duration': meeting_duration,
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message
        })

        response = self.extract_json_from_response(response.content)
        
        logger.info(f"Detected confirmation response: {response}")
        return response
    
    def check_context_relevance(self, participant_name, participant_role, meeting_duration, conversation_history, user_message):
        """
        Check if a message is relevant to the interview scheduling context.
        
        Args:
            participant_name (str): Name of the participant
            participant_role (str): Role of the participant
            meeting_duration (int): Duration of the meeting in minutes
            conversation_history (str): Previous conversation history
            user_message (str): Current message to analyze
            
        Returns:
            dict: Dictionary containing 'is_relevant' boolean and 'context_type' string
        """
        try:
            json1 = """
                    {{
                        "is_relevant": false,
                        "context_type": "technical"
                    }}
                    """
            PROMPT_TEMPLATE = f"""
                **## Role**

                Act as an expert conversational AI designed to manage contextually relevant interactions for scheduling interviews. 

                **## Task**

                Your goal is to assess if a user’s latest message, {{user_message}}, aligns with the intent of scheduling an interview, based on the provided conversation history and other variables. If the message is unrelated to interview scheduling, categorize it accordingly and respond in JSON format.

                **## Specifics**

                - Evaluate if the {{user_message}} is contextually aligned with interview scheduling intent.
                - Reference the {{conversation_history}} for any clues regarding prior conversation topics.
                - Use the {{participant_name}}, {{participant_role}}, and {{meeting_duration}} variables as context markers when analyzing the message.
                - If the {{user_message}} is unrelated to interview scheduling, categorize the message type and set `"is_relevant"` to `false`.

                **## Output Requirements**

                Output a JSON response structured as follows:

                - `"is_relevant"`: Boolean indicating if the message is relevant to interview scheduling.
                - `"context_type"`: A string categorizing the type of unrelated context if `"is_relevant"` is `false`.
d database
            self.scheduler.conversation
                **## Output Format Example**

                If the {{user_message}} is unrelated to interview scheduling, output in this JSON format:

                ```json
                {json1}
                ```

                **## Notes**

                - Ensure {{user_message}} is evaluated in light of the entire {{conversation_history}}.
                - The `"context_type"` should describe the general nature of unrelated messages (e.g., "technical," "general inquiry," etc.).
                - Prioritize a concise, clear assessment over exhaustive analysis, focusing on whether the user’s message aligns with interview scheduling.

                """

            prompt_template = PromptTemplate(
                input_variables=[
                    'participant_name',
                    'participant_role',
                    'meeting_duration',
                    'conversation_history',
                    'user_message'
                ],
                template=PROMPT_TEMPLATE
            )

            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.7,
            )

            chain = prompt_template | llm_model

            response = chain.invoke({
                'participant_name': participant_name,
                'participant_role': participant_role,
                'meeting_duration': meeting_duration,
                'conversation_history': conversation_history,
                'user_message': user_message
            })

            llm_output = response.content

            # Use regex to find JSON portion in response content
            json_match = re.search(r'\{.*\}', llm_output, re.DOTALL)
            if json_match:
                clean_json = json_match.group(0)
                data = json.loads(clean_json)
            else:
                raise ValueError("No JSON found in response")

            result = {
                'is_relevant': data.get("is_relevant"),
                'context_type': data.get("context_type")
            }

            print({
                'is_relevant': result['is_relevant'],
                'context_type': result['context_type']
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Error in check_context_relevance: {str(e)}")
            # Return a safe default indicating the message is relevant to avoid disrupting the conversation
            return {
                'is_relevant': True,
                'context_type': 'scheduling'
            }
        
    def extract_meeting_duration(self, user_message):
        """
        Extract the meeting duration (in minutes) from the user's message.
        
        Expected Output Format:
        ```json
        {
            "meeting_duration": 30
        }
        ```
        """
        try:
            json_template = """
                {{
                    "meeting_duration": 30
                }}
            """
            PROMPT_TEMPLATE = f"""
                Extract the meeting duration in minutes from the following message:
                "{user_message}"

                The output should be a JSON in the format:
                ```json
                {json_template}
                ```
                where "meeting_duration" is an integer representing the duration in minutes.
            """

            prompt = PromptTemplate(
                input_variables=['user_message'],
                template=PROMPT_TEMPLATE
            )

            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.3,
            )

            chain = prompt | llm_model
            response = chain.invoke({'user_message': user_message})
            llm_output = response.content.strip()

            logger.info(f"Extracted meeting duration response: {llm_output}")

            parsed_data = self.extract_json_from_response(llm_output)

            if not parsed_data or 'meeting_duration' not in parsed_data:
                logger.error("Failed to extract meeting duration from the response.")
                return None

            duration = parsed_data['meeting_duration']
            if not isinstance(duration, int) or duration <= 0:
                logger.error("Meeting duration is not a positive integer.")
                return None

            return duration

        except Exception as e:
            logger.error(f"Error in extract_meeting_duration: {str(e)}")
            return None
    
    def extract_interviewee_name(self, user_message):
        """
        Extracts the interviewee's name from the cancellation message.

        Args:
            user_message (str): The user's cancellation message.

        Returns:
            str: The extracted interviewee's name if found, else None.
        """
        try:
            json1="""
                {{
                    "interviewee_name": "Name here"
                }}
            """
            
            PROMPT_TEMPLATE = f"""
                Extract the interviewee's name from the following cancellation message.

                Message: "{user_message}"

                ### Extraction
                {json1}
            """
            prompt_template = PromptTemplate(
                input_variables=['user_message'],
                template=PROMPT_TEMPLATE
            )

            llm_model = ChatGoogleGenerativeAI(
                model="gemini-1.5-flash",
                temperature=0.3,
            )

            chain = prompt_template | llm_model
            response = chain.invoke({'user_message': user_message})
            llm_output = response.content.strip()

            # Extract JSON from the response
            parsed_data = self.extract_json_from_response(llm_output)
            interviewee_name = parsed_data.get("interviewee_name")

            if interviewee_name:
                return interviewee_name.strip()
            return None
        except Exception as e:
            logger.error(f"Error extracting interviewee name: {str(e)}")
            return None