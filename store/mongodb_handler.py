# mongodb_handler.py

from pymongo import MongoClient
from datetime import datetime
import pytz
import logging

logger = logging.getLogger(__name__)

class MongoDBHandler:
    def __init__(self, uri, db_name):
        self.client = MongoClient(uri)
        self.db = self.client[db_name]
        self.conversations = self.db.conversations

    def create_conversation(self, conversation_data):
        """
        Inserts a new conversation document into the database.
        """
        try:
            self.conversations.insert_one(conversation_data)
            logger.info(f"Conversation {conversation_data['conversation_id']} inserted into MongoDB.")
        except Exception as e:
            logger.error(f"Error inserting conversation into MongoDB: {e}")
            raise

    def get_conversation(self, conversation_id):
        """
        Retrieves a conversation document by conversation_id.
        """
        try:
            conversation = self.conversations.find_one({'conversation_id': conversation_id})
            if conversation:
                logger.info(f"Conversation {conversation_id} retrieved from MongoDB.")
            else:
                logger.warning(f"Conversation {conversation_id} not found in MongoDB.")
            return conversation
        except Exception as e:
            logger.error(f"Error retrieving conversation from MongoDB: {e}")
            raise

    def get_all_conversations(self):
        """
        Retrieves all conversation documents.
        """
        try:
            conversations = list(self.conversations.find())
            logger.info(f"Retrieved all conversations from MongoDB.")
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving all conversations from MongoDB: {e}")
            raise

    def update_conversation(self, conversation_id, update_data, filter_data=None):
        """
        Updates a conversation document with new data.
        If filter_data is provided, it uses it as an additional filter.
        """
        try:
            if filter_data:
                query = {'conversation_id': conversation_id}
                query.update(filter_data)
            else:
                query = {'conversation_id': conversation_id}
            
            self.conversations.update_one(query, {'$set': update_data})
            logger.info(f"Conversation {conversation_id} updated in MongoDB.")
        except Exception as e:
            logger.error(f"Error updating conversation in MongoDB: {e}")
            raise

    def delete_conversation(self, conversation_id):
        """
        Deletes a conversation document by conversation_id.
        """
        try:
            self.conversations.delete_one({'conversation_id': conversation_id})
            logger.info(f"Conversation {conversation_id} deleted from MongoDB.")
        except Exception as e:
            logger.error(f"Error deleting conversation from MongoDB: {e}")
            raise

    def delete_conversations_past_scheduled_time(self):
        """
        Deletes conversations where all scheduled times have passed.
        """
        try:
            current_time = datetime.now(pytz.UTC)
            result = self.conversations.delete_many({
                'interviewees': {
                    '$elemMatch': {
                        'scheduled_slot.end_time': {'$lt': current_time.isoformat()}
                    }
                }
            })
            logger.info(f"Deleted {result.deleted_count} conversations past scheduled time from MongoDB.")
        except Exception as e:
            logger.error(f"Error deleting past conversations from MongoDB: {e}")
            raise
    def find_conversation_by_number(self, number: str):
        """
        Finds a single conversation that involves the given phone number, either as an interviewer or interviewee.
        Returns the conversation document if found, otherwise None.
        """
        try:
            conversation = self.conversations.find_one({
                '$or': [
                    {'interviewer.number': number},
                    {'interviewees.number': number}
                ]
            })
            if conversation:
                logger.info(f"Found conversation containing number: {number}")
            else:
                logger.warning(f"No conversation found containing number: {number}")
            return conversation
        except Exception as e:
            logger.error(f"Error retrieving conversation by number {number} from MongoDB: {e}")
            raise

