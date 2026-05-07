import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './PdfToImages.css';

const PdfToImages = ({
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
    <div className="pdf-to-images-form">
      <h2>PDF to Images + Excel</h2>
      <p className="form-description">
        Crops each question into its own PNG and runs full Q&amp;A extraction. Downloads a ZIP
        containing all question images and <code>extraction_results.xlsx</code> with question
        text and answers. Provide the Answers PDF to populate the answer column.
      </p>

      <div className="file-input-group">
        <label htmlFor="q-pdf">Questions PDF *</label>
        <input
          ref={questionsInputRef}
          id="q-pdf"
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
        <label htmlFor="a-pdf">
          Answers PDF{' '}
          <span style={{ fontWeight: 'normal', color: '#888' }}>(optional)</span>
        </label>
        <input
          ref={answersInputRef}
          id="a-pdf"
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
          <strong>Done!</strong> ZIP downloaded — contains cropped question images and{' '}
          <code>extraction_results.xlsx</code> with question text and answers.
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        onClick={onSubmit}
        className="submit-button"
      >
        {loading ? 'Processing...' : 'Extract & Download ZIP'}
      </button>
    </div>
  );
};

export default PdfToImages;
