'use client';

// src/app/components/SchedulingInterface.tsx
import React, { useState, useRef } from 'react';
import { Upload, AlertCircle, FileText } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import axios, { AxiosError } from 'axios';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:5000/api';
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || '';

interface UploadResponse {
  conversations_created: number;
  conversations_failed: number;
  results: Array<{
    status: 'success' | 'failed';
    error?: string;
    interviewer?: string;
  }>;
}

const SchedulingInterface = () => {
  const [file, setFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [submissionStatus, setSubmissionStatus] = useState<'idle' | 'submitting' | 'success' | 'error'>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile && droppedFile.name.endsWith('.csv')) {
      setFile(droppedFile);
      setErrorMessage('');
    } else {
      setErrorMessage('Please upload a CSV file');
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      const selectedFile = e.target.files[0];
      if (selectedFile.name.endsWith('.csv')) {
        setFile(selectedFile);
        setErrorMessage('');
      } else {
        setErrorMessage('Please upload a CSV file');
        setFile(null);
      }
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setErrorMessage('Please select a file to upload');
      return;
    }

    setSubmissionStatus('submitting');
    const formData = new FormData();
    formData.append('file', file);

    try {
      const response = await axios.post<UploadResponse>(`${API_BASE_URL}/upload-csv`, formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
          'x-api-key': API_KEY
        }
      });

      const { data } = response;

      if (data.conversations_failed > 0) {
        const failedResults = data.results.filter(r => r.status === 'failed');
        const errorMsg = failedResults
          .map(r => r.error || 'Unknown error')
          .join(', ');
        throw new Error(`Failed to initialize some conversations: ${errorMsg}`);
      }

      setSubmissionStatus('success');
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      alert(`Successfully initialized ${data.conversations_created} conversations`);
    } catch (error) {
      console.error('Error uploading CSV:', error);
      
      let errorMsg = 'Failed to upload CSV file. Please check the file format and try again.';
      
      if (error instanceof AxiosError) {
        errorMsg = error.response?.data?.error || error.message;
      } else if (error instanceof Error) {
        errorMsg = error.message;
      }
      
      setErrorMessage(errorMsg);
      setSubmissionStatus('error');
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <Card className="max-w-2xl mx-auto">
        <CardHeader>
          <CardTitle className="text-black">Upload Interview Schedule CSV</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-6">
            <div
              className={`
                relative border-2 border-dashed rounded-lg p-8
                text-center cursor-pointer transition-colors
                ${dragActive ? 'border-black bg-gray-50' : 'border-gray-300 hover:border-gray-400'}
              `}
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv"
                onChange={handleFileChange}
                className="hidden"
              />
              
              <div className="flex flex-col items-center space-y-4">
                <Upload className="h-12 w-12 text-gray-400" />
                <div className="flex flex-col space-y-2">
                  <span className="text-gray-600">
                    {file ? (
                      <div className="flex items-center justify-center space-x-2">
                        <FileText className="w-4 h-4" />
                        <span>{file.name}</span>
                      </div>
                    ) : (
                      <>
                        <span className="font-semibold text-black">Click to upload</span> or drag and drop
                      </>
                    )}
                  </span>
                  <span className="text-sm text-gray-500">CSV files only</span>
                </div>
              </div>
            </div>

            {errorMessage && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{errorMessage}</AlertDescription>
              </Alert>
            )}

            <button
              type="submit"
              disabled={!file || submissionStatus === 'submitting'}
              className="w-full bg-black text-white py-2 px-4 rounded-lg hover:bg-gray-800 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              {submissionStatus === 'submitting' ? 'Uploading...' : 'Upload and Initialize Interviews'}
            </button>
          </form>

          <div className="mt-8">
            <h3 className="font-semibold mb-2 text-black">Required CSV Format:</h3>
            <p className="text-sm text-gray-600 space-y-1">
              Your CSV file must include the following columns:
            </p>
            <ul className="text-sm text-gray-600 list-disc list-inside mt-2">
              <li>interviewer_name, interviewer_number, interviewer_email</li>
              <li>interviewee_name, interviewee_number, interviewee_email</li>
              <li>jd_title, meeting_duration, superior_flag</li>
              <li>role_to_contact_name, role_to_contact_number, role_to_contact_email</li>
              <li>company_details</li>
            </ul>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default SchedulingInterface;