// frontend/src/components/MonitoringDashboard.tsx
'use client';

import React, { useState, useEffect } from 'react';
import { Calendar, Clock, User, AlertCircle, Mail, Phone, Loader2, Trash2 } from 'lucide-react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { 
  getActiveConversations, 
  getCompletedConversations, 
  getAttentionFlags, 
  deleteConversation, 
  resolveAttentionFlag,
  getScheduledInterviews 
} from '@/services/api';
import type { ActiveConversation, ScheduledInterview, AttentionFlag } from '@/types/api';

// Type guard to ensure flag conforms to AttentionFlag interface
const isValidAttentionFlag = (flag: unknown): flag is AttentionFlag => {
  if (typeof flag !== 'object' || flag === null) {
    return false;
  }

  const flagObj = flag as Record<string, unknown>;

  return (
    typeof flagObj.id === 'string' &&
    typeof flagObj.conversation_id === 'string' &&
    typeof flagObj.message === 'string' &&
    typeof flagObj.severity === 'string' &&
    ['low', 'medium', 'high'].includes(flagObj.severity) &&
    typeof flagObj.created_at === 'string' &&
    typeof flagObj.resolved === 'boolean'
  );
};

const MonitoringDashboard = () => {
  const [activeConversations, setActiveConversations] = useState<ActiveConversation[]>([]);
  const [completedConversations, setCompletedConversations] = useState<ActiveConversation[]>([]);
  const [scheduledInterviews, setScheduledInterviews] = useState<ScheduledInterview[]>([]);
  const [attentionFlags, setAttentionFlags] = useState<AttentionFlag[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleteStatus, setDeleteStatus] = useState<{ [key: string]: 'idle' | 'deleting' | 'success' | 'error' }>({});
  const [resolveStatus, setResolveStatus] = useState<{ [key: string]: 'idle' | 'resolving' | 'success' | 'error' }>({});

  useEffect(() => {
    fetchData();
    // Set up polling every 30 seconds
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchData = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [activeConvs, completedConvs, flags, scheduled] = await Promise.all([
        getActiveConversations().catch(() => []),
        getCompletedConversations().catch(() => []),
        getAttentionFlags().catch(() => []),
        getScheduledInterviews().catch(() => [])
      ]);
      
      setActiveConversations(activeConvs);
      setCompletedConversations(completedConvs);
      setAttentionFlags(flags.filter(isValidAttentionFlag)); // Apply type guard
      setScheduledInterviews(scheduled);
    } catch (error) {
      console.error('Error fetching data:', error);
      setError('Failed to fetch monitoring data. Please try again later.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleDelete = async (conversationId: string) => {
    setDeleteStatus(prev => ({ ...prev, [conversationId]: 'deleting' }));
    try {
      await deleteConversation(conversationId);
      setActiveConversations(prev => prev.filter(conv => conv.id !== conversationId));
      setCompletedConversations(prev => prev.filter(conv => conv.id !== conversationId));
      setDeleteStatus(prev => ({ ...prev, [conversationId]: 'success' }));
    } catch (error) {
      console.error('Error deleting conversation:', error);
      setDeleteStatus(prev => ({ ...prev, [conversationId]: 'error' }));
      alert('Failed to delete conversation. Please try again.');
    }
  };

  const handleResolveFlag = async (flagId: string) => {
    setResolveStatus(prev => ({ ...prev, [flagId]: 'resolving' }));
    try {
      await resolveAttentionFlag(flagId);
      setResolveStatus(prev => ({ ...prev, [flagId]: 'success' }));
      // Refresh data to reflect resolved flags
      fetchData();
    } catch (error) {
      console.error('Error resolving attention flag:', error);
      setResolveStatus(prev => ({ ...prev, [flagId]: 'error' }));
      alert('Failed to resolve attention flag. Please try again.');
    }
  };

  const formatDateTime = (dateStr: string) => {
    const options: Intl.DateTimeFormatOptions = {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      timeZoneName: 'short'
    };
    return new Date(dateStr).toLocaleString(undefined, options);
  };

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'scheduled':
        return 'text-green-700';
      case 'pending':
        return 'text-yellow-700';
      case 'cancelled':
        return 'text-red-700';
      default:
        return 'text-gray-900';
    }
  };

  if (error) {
    return (
      <Alert variant="destructive" className="m-4">
        <AlertCircle className="h-4 w-4 text-red-700" />
        <AlertDescription className="text-gray-900">{error}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <div className="max-w-7xl mx-auto">
        <h1 className="text-3xl font-bold text-gray-900 mb-8">Interview Monitoring Dashboard</h1>
        
        <Tabs defaultValue="conversations" className="space-y-8">
          <TabsList className="grid w-full grid-cols-4 gap-4">
            <TabsTrigger value="conversations" className="text-gray-900">
              Active Conversations ({activeConversations.length})
            </TabsTrigger>
            <TabsTrigger value="completed" className="text-gray-900">
              Completed Conversations ({completedConversations.length})
            </TabsTrigger>
            <TabsTrigger value="scheduled" className="text-gray-900">
              Scheduled Interviews ({scheduledInterviews.length})
            </TabsTrigger>
            <TabsTrigger value="attention" className="relative text-gray-900">
              Attention Required
              {attentionFlags.length > 0 && (
                <span className="absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full w-5 h-5 flex items-center justify-center">
                  {attentionFlags.length}
                </span>
              )}
            </TabsTrigger>
          </TabsList>

          {/* Active Conversations Tab */}
          <TabsContent value="conversations" className="space-y-4">
            {isLoading ? (
              <div className="flex justify-center items-center py-12">
                <Loader2 className="h-8 w-8 animate-spin text-gray-900" />
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {activeConversations.map((conversation) => (
                  <Card key={conversation.id}>
                    <CardHeader className="flex justify-between items-center">
                      <CardTitle className="flex items-center gap-2 text-lg text-gray-900">
                        <User className="h-5 w-5 text-gray-900" />
                        {conversation.interviewer_name}
                      </CardTitle>
                      <button
                        onClick={() => handleDelete(conversation.id)}
                        disabled={deleteStatus[conversation.id] === 'deleting'}
                        className="text-red-500 hover:text-red-700"
                        aria-label="Delete Conversation"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      <div className="space-y-2">
                        <div className="flex items-center gap-2 text-sm text-gray-900">
                          <Mail className="h-4 w-4 text-gray-900" />
                          {conversation.interviewer_email}
                        </div>
                        <div className="flex items-center gap-2 text-sm text-gray-900">
                          <Phone className="h-4 w-4 text-gray-900" />
                          {conversation.interviewer_number}
                        </div>
                      </div>
                      <div className="border-t pt-4">
                        <h4 className="font-medium mb-2 text-gray-900">Interviewees ({conversation.interviewees.length})</h4>
                        <div className="space-y-2">
                          {conversation.interviewees.map((interviewee) => (
                            <div key={interviewee.id} className="text-sm text-gray-900">
                              <span className="font-medium text-gray-900">{interviewee.name}</span>
                              <span className={`ml-2 ${getStatusColor(interviewee.status)}`}>
                                ({interviewee.status})
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="text-xs text-gray-900">
                        Last Activity: {formatDateTime(conversation.last_activity)}
                      </div>
                      {/* Attention Flags for this Conversation */}
                      {conversation.attention_flags && conversation.attention_flags.length > 0 && (
                        <div className="mt-2">
                          <h5 className="text-sm font-semibold text-red-600">Attention Flags:</h5>
                          <ul className="list-disc list-inside text-xs text-red-600">
                            {conversation.attention_flags.map(flag => (
                              <li key={flag.id} className="flex justify-between items-center">
                                <span>{flag.message}</span>
                                <button
                                  onClick={() => handleResolveFlag(flag.id)} // Safe to use without '!'
                                  disabled={resolveStatus[flag.id] === 'resolving'}
                                  className="text-sm text-blue-500 hover:text-blue-700"
                                >
                                  Resolve
                                </button>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))}
                {activeConversations.length === 0 && (
                  <div className="col-span-4 text-center py-12 text-gray-900">
                    No active conversations at the moment
                  </div>
                )}
              </div>
            )}
          </TabsContent>

          {/* Completed Conversations Tab */}
          <TabsContent value="completed" className="space-y-4">
            {isLoading ? (
              <div className="flex justify-center items-center py-12">
                <Loader2 className="h-8 w-8 animate-spin text-gray-900" />
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {completedConversations.map((conversation) => (
                  <Card key={conversation.id}>
                    <CardHeader className="flex justify-between items-center">
                      <CardTitle className="flex items-center gap-2 text-lg text-gray-900">
                        <User className="h-5 w-5 text-gray-900" />
                        {conversation.interviewer_name}
                      </CardTitle>
                      <button
                        onClick={() => handleDelete(conversation.id)}
                        disabled={deleteStatus[conversation.id] === 'deleting'}
                        className="text-red-500 hover:text-red-700"
                        aria-label="Delete Conversation"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      <div className="space-y-2">
                        <div className="flex items-center gap-2 text-sm text-gray-900">
                          <Mail className="h-4 w-4 text-gray-900" />
                          {conversation.interviewer_email}
                        </div>
                        <div className="flex items-center gap-2 text-sm text-gray-900">
                          <Phone className="h-4 w-4 text-gray-900" />
                          {conversation.interviewer_number}
                        </div>
                      </div>
                      <div className="border-t pt-4">
                        <h4 className="font-medium mb-2 text-gray-900">Interviewees ({conversation.interviewees.length})</h4>
                        <div className="space-y-2">
                          {conversation.interviewees.map((interviewee) => (
                            <div key={interviewee.id} className="text-sm text-gray-900">
                              <span className="font-medium text-gray-900">{interviewee.name}</span>
                              <span className={`ml-2 ${getStatusColor(interviewee.status)}`}>
                                ({interviewee.status})
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="text-xs text-gray-900">
                        Completed At: {formatDateTime(conversation.completed_at)}
                      </div>
                      {/* Attention Flags for this Conversation */}
                      {conversation.attention_flags && conversation.attention_flags.length > 0 && (
                        <div className="mt-2">
                          <h5 className="text-sm font-semibold text-red-600">Attention Flags:</h5>
                          <ul className="list-disc list-inside text-xs text-red-600">
                            {conversation.attention_flags.map(flag => (
                              <li key={flag.id} className="flex justify-between items-center">
                                <span>{flag.message}</span>
                                <button
                                  onClick={() => handleResolveFlag(flag.id)} // Safe to use without '!'
                                  disabled={resolveStatus[flag.id] === 'resolving'}
                                  className="text-sm text-blue-500 hover:text-blue-700"
                                >
                                  Resolve
                                </button>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))}
                {completedConversations.length === 0 && (
                  <div className="col-span-4 text-center py-12 text-gray-900">
                    No completed conversations at the moment
                  </div>
                )}
              </div>
            )}
          </TabsContent>

          {/* Scheduled Interviews Tab */}
          <TabsContent value="scheduled" className="space-y-4">
            {isLoading ? (
              <div className="flex justify-center items-center py-12">
                <Loader2 className="h-8 w-8 animate-spin text-gray-900" />
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {scheduledInterviews.map((interview: ScheduledInterview) => (
                  <Card key={interview.id}>
                    <CardHeader>
                      <CardTitle className="flex items-center gap-2 text-lg text-gray-900">
                        <Calendar className="h-5 w-5 text-gray-900" />
                        {interview.title}
                      </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      <div className="flex items-center gap-2 text-sm font-medium text-gray-900">
                        <Clock className="h-4 w-4 text-gray-900" />
                        {formatDateTime(interview.scheduled_time)}
                      </div>
                      <div className="border-t pt-4 space-y-2">
                        <div className="space-y-1">
                          <h4 className="font-medium text-gray-900">Interviewer</h4>
                          <div className="text-sm text-gray-900">{interview.interviewer_name}</div>
                          <div className="text-sm text-gray-900">{interview.interviewer_email}</div>
                        </div>
                        <div className="space-y-1">
                          <h4 className="font-medium text-gray-900">Interviewee</h4>
                          <div className="text-sm text-gray-900">{interview.interviewee_name}</div>
                          <div className="text-sm text-gray-900">{interview.interviewee_email}</div>
                        </div>
                      </div>
                      <div className="text-sm text-gray-900">
                        <span className={`font-medium ${getStatusColor(interview.status)}`}>
                          {interview.status}
                        </span>
                      </div>
                    </CardContent>
                  </Card>
                ))}
                {scheduledInterviews.length === 0 && (
                  <div className="col-span-4 text-center py-12 text-gray-900">
                    No scheduled interviews at the moment
                  </div>
                )}
              </div>
            )}
          </TabsContent>

          {/* Attention Required Tab */}
          <TabsContent value="attention" className="space-y-4">
            {isLoading ? (
              <div className="flex justify-center items-center py-12">
                <Loader2 className="h-8 w-8 animate-spin text-gray-900" />
              </div>
            ) : (
              <div className="space-y-4">
                {attentionFlags.map((flag) => (
                  <Alert key={flag.id} variant="destructive">
                    <AlertCircle className="h-4 w-4 text-gray-900" />
                    <AlertDescription className="flex justify-between items-center text-gray-900">
                      <span>{flag.message}</span>
                      <div className="flex items-center space-x-2">
                        <button
                          onClick={() => handleResolveFlag(flag.id)} // Safe to use without '!'
                          disabled={resolveStatus[flag.id] === 'resolving'}
                          className="text-sm text-blue-500 hover:text-blue-700"
                          aria-label={`Resolve attention flag: ${flag.message}`}
                        >
                          Resolve
                        </button>
                        <span className="text-sm text-gray-900">
                          {formatDateTime(flag.created_at)}
                        </span>
                      </div>
                    </AlertDescription>
                  </Alert>
                ))}
                {attentionFlags.length === 0 && (
                  <div className="text-center py-12 text-gray-900">
                    No attention flags at the moment
                  </div>
                )}
              </div>
            )}
          </TabsContent>
        </Tabs> {/* Properly closed the Tabs component */}

        <div className="mt-8 flex justify-end">
          <button
            onClick={fetchData}
            className="flex items-center gap-2 px-4 py-2 text-sm text-gray-900 hover:text-gray-900"
          >
            <Loader2 className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh Data
          </button>
        </div>
      </div>
    </div>
  );
};

export default MonitoringDashboard;
