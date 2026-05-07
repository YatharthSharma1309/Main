import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './PdfExtractor.css';

const PdfExtractor = ({
  questionsPdf,
  answersPdf,
  loading,
  error,
  success,
  onPdfChange,
  onSubmit,
  canSubmit,
}) => {
  const questionsInputRef = useRef(null);
  const answersInputRef = useRef(null);

  return (
    <div className="pdf-extractor-form">
      <h2>Extract Q&A to Excel</h2>

      <div className="file-input-group">
        <label htmlFor="questions">Questions PDF *</label>
        <input
          ref={questionsInputRef}
          id="questions"
          type="file"
          accept=".pdf"
          onChange={(e) => onPdfChange(e, 'Questions PDF', 'questions')}
          disabled={loading}
          className="file-input"
        />
        {questionsPdf && (
          <div className="file-info">
            {questionsPdf.name} ({(questionsPdf.size / 1024).toFixed(1)} KB)
          </div>
        )}
      </div>

      <div className="file-input-group">
        <label htmlFor="answers">Answers PDF <span style={{fontWeight:'normal',color:'#888'}}>(optional — uses questions PDF if omitted)</span></label>
        <input
          ref={answersInputRef}
          id="answers"
          type="file"
          accept=".pdf"
          onChange={(e) => onPdfChange(e, 'Answers PDF', 'answers')}
          disabled={loading}
          className="file-input"
        />
        {answersPdf && (
          <div className="file-info">
            {answersPdf.name} ({(answersPdf.size / 1024).toFixed(1)} KB)
          </div>
        )}
      </div>

      {error && <div className="error-message">{error}</div>}

      {success && (
        <div className="success-message">
          Successfully extracted Q&A. Excel downloaded.
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        onClick={onSubmit}
        className="submit-button"
      >
        {loading ? 'Processing...' : 'Extract & Download Excel'}
      </button>
    </div>
  );
};

export default PdfExtractor;
